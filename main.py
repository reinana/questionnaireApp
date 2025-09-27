# ----------------------------------------------------------------
# 1. imports
# ----------------------------------------------------------------
import os, re, time
import functions_framework
import firebase_admin
from firebase_admin import auth, firestore
import google.generativeai as genai
from flask import jsonify
from google.cloud import vision, storage
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ----------------------------------------------------------------
# 2. init
# ----------------------------------------------------------------
try:
    firebase_admin.get_app()
except ValueError:
    firebase_admin.initialize_app()

db = firestore.client()

# Gemini
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# Vision / Storage
vision_client = vision.ImageAnnotatorClient()
storage_client = storage.Client()
TEMP_BUCKET = os.environ.get("TEMP_BUCKET")  # 例: questionnaire-app-472614-vision-tmp

# Sheets
SERVICE_ACCOUNT_FILE = os.environ.get("SHEETS_SA_JSON", "credentials.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ----------------------------------------------------------------
# 3. utils
# ----------------------------------------------------------------
def extract_spreadsheet_id(url: str) -> str:
    """GoogleスプレッドシートURLからIDを抽出（URLでなければそのまま返す）"""
    if not url:
        return ""
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", url)
    return m.group(1) if m else url.strip()

def verify_token(request) -> str:
    """Authorization: Bearer <idToken> 検証して uid を返す"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise ValueError("認証トークンがありません。")
    id_token = auth_header.split(" ", 1)[1]
    decoded = auth.verify_id_token(id_token)
    return decoded["uid"]

def ocr_image_inline(file_bytes: bytes) -> str:
    """画像（JPEG/PNG等）を Vision 同期OCR"""
    img = vision.Image(content=file_bytes)
    resp = vision_client.document_text_detection(image=img)
    if resp.error.message:
        print(f"[VisionError] {resp.error.message}")
        raise RuntimeError(f"Vision API error: {resp.error.message}")
    return (resp.full_text_annotation.text or "").strip()

def ocr_pdf_via_gcs(file_bytes: bytes) -> str:
    """PDF → GCS へ置く → Vision 非同期 → 出力JSONをjson.loadsで読む → 結合テキスト返却"""
    if not TEMP_BUCKET:
        raise RuntimeError("TEMP_BUCKET が設定されていません。")

    ts = int(time.time() * 1000)
    src_name = f"uploads/{ts}.pdf"
    out_prefix = f"vision-out/{ts}/"   # ← 重要：末尾のスラッシュ

    bucket = storage_client.bucket(TEMP_BUCKET)

    # 1) PDFアップロード
    src_blob = bucket.blob(src_name)
    src_blob.upload_from_string(file_bytes, content_type="application/pdf")
    gcs_src_uri = f"gs://{TEMP_BUCKET}/{src_name}"
    print(f"[PDF OCR] uploaded: {gcs_src_uri}")

    # 2) Vision 非同期
    gcs_dst = vision.GcsDestination(uri=f"gs://{TEMP_BUCKET}/{out_prefix}")
    gcs_src = vision.GcsSource(uri=gcs_src_uri)
    feature = vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)
    input_config = vision.InputConfig(gcs_source=gcs_src, mime_type="application/pdf")
    output_config = vision.OutputConfig(gcs_destination=gcs_dst)

    req = vision.AsyncAnnotateFileRequest(
        features=[feature], input_config=input_config, output_config=output_config
    )
    op = vision_client.async_batch_annotate_files(requests=[req])
    op.result(timeout=600)
    print(f"[PDF OCR] async finished. output prefix: gs://{TEMP_BUCKET}/{out_prefix}")

    # 3) 出力JSONを読む（複数あり得る）
    blobs = list(storage_client.list_blobs(TEMP_BUCKET, prefix=out_prefix))
    json_blobs = [b for b in blobs if b.name.endswith(".json")]
    print(f"[PDF OCR] json files: {len(json_blobs)}")

    if not json_blobs:
        # Vision 側の出力が無い場合に早期通知
        raise RuntimeError(f"OCR結果(JSON)が見つかりません: gs://{TEMP_BUCKET}/{out_prefix}")

    texts = []
    for b in json_blobs:
        data = json.loads(b.download_as_bytes())
        for r in data.get("responses", []):
            txt = r.get("fullTextAnnotation", {}).get("text", "")
            if txt:
                texts.append(txt)

    # 4) 掃除（best-effort）
    try:
        src_blob.delete()
        for b in blobs:
            b.delete()
    except Exception as e:
        print(f"[PDF OCR] cleanup warning: {e}")

    return "\n".join(texts).strip()

# ----------------------------------------------------------------
# 4. Functions
# ----------------------------------------------------------------
def _clamp(s: str, max_chars: int = 120_000) -> str:
    return s if len(s) <= max_chars else s[:max_chars]

@functions_framework.http
def analyze_survey_template(request):
    headers = {"Access-Control-Allow-Origin": "*"}
    if request.method == "OPTIONS":
        headers.update({
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "3600",
        })
        return ("", 204, headers)

    try:
        uid = verify_token(request)

        template_name = (request.form.get("template_name") or "").strip()
        if not template_name:
            raise ValueError("テンプレート名が指定されていません。")

        spreadsheet_url = (request.form.get("spreadsheet_url") or "").strip()
        if not spreadsheet_url:
            raise ValueError("スプレッドシートURLが指定されていません。")
        spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)

        uploaded_file = request.files.get("file")
        if not uploaded_file:
            raise ValueError("ファイルが含まれていません。")
        file_bytes = uploaded_file.read()
        if not file_bytes:
            raise ValueError("アップロードされたファイルが空でした。")

        mime = (uploaded_file.mimetype or "").lower()
        fname = (uploaded_file.filename or "").lower()
        print(f"[ANALYZE] file: {fname} ({mime}), size={len(file_bytes)}")

        # --- OCR（PDF優先） ---
        if mime == "application/pdf" or fname.endswith(".pdf"):
            raw_text = ocr_pdf_via_gcs(file_bytes)
        else:
            raw_text = ocr_image_inline(file_bytes)

        if not raw_text:
            raise ValueError("OCRでテキストを抽出できませんでした。")

        # --- Gemini へ渡すテキストを制限（過大入力での 4xx/5xx 防止）---
        raw_for_gemini = _clamp(raw_text)

        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt_for_questions = """
        あなたはアンケート設計を分析する専門家です。
        以下のOCRテキストから、回答欄に相当する質問項目のみを抽出してください。
        出力は改行区切りのリストとしてください。
        """
        gresp = model.generate_content([prompt_for_questions, raw_for_gemini])

        # gresp.text が空の場合のフォールバック
        prompt_items = ""
        if getattr(gresp, "text", None):
            prompt_items = gresp.text.strip()
        else:
            try:
                parts = []
                for cand in getattr(gresp, "candidates", []) or []:
                    for p in getattr(cand, "content", {}).parts or []:
                        t = getattr(p, "text", "")
                        if t:
                            parts.append(t)
                prompt_items = "\n".join(parts).strip()
            except Exception as e:
                print(f"[ANALYZE] gemini fallback parse failed: {e}")
                prompt_items = ""

        # --- 保存 ---
        ref = db.collection("users").document(uid).collection("templates").document(template_name)
        ref.set({
            "items": prompt_items,
            "spreadsheetId": spreadsheet_id,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })

        return (f"テンプレート「{template_name}」を作成しました。", 200, headers)

    except ValueError as e:
        return (str(e), 400, headers)
    except Exception as e:
        # ここで “どの段階で失敗したか” が分かるようにログ出力を増やす
        print(f"[ANALYZE][ERROR] {e}")
        return ("テンプレート作成中にエラーが発生しました。", 500, headers)

@functions_framework.http
def ocr_and_write_sheet(request):
    """
    テンプレートを使って複数ファイル（PDF/画像）から回答抽出→Sheetsへ書込
    1行目: ヘッダ（質問項目）
    2行目以降: 回答
    """
    headers = {"Access-Control-Allow-Origin": "*"}
    if request.method == "OPTIONS":
        headers.update({
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "3600",
        })
        return ("", 204, headers)

    try:
        uid = verify_token(request)

        template_name = (request.form.get("template_name") or "").strip()
        if not template_name:
            raise ValueError("テンプレート名が指定されていません。")

        # 入力ファイル
        uploaded_files = request.files.getlist("files")
        if not uploaded_files:
            raise ValueError("ファイルが含まれていません。")

        # テンプレート取得
        tref = db.collection("users").document(uid).collection("templates").document(template_name)
        tdoc = tref.get()
        if not tdoc.exists:
            raise ValueError(f"テンプレート「{template_name}」が見つかりません。")
        tdata = tdoc.to_dict()

        header_row = [q.strip() for q in (tdata.get("items") or "").splitlines() if q.strip()]
        spreadsheet_id = tdata.get("spreadsheetId")
        if not spreadsheet_id:
            raise ValueError("テンプレートに紐付いたスプレッドシートIDが存在しません。")

        # Sheets クライアント
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        sheets = build("sheets", "v4", credentials=creds)

        # ヘッダ行を書き込み
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range="A1",
            valueInputOption="RAW",
            body={"values": [header_row]},
        ).execute()

        rows = []
        for f in uploaded_files:
            fb = f.read()
            if not fb:
                rows.append(["N/A"] * len(header_row))
                continue

            mime = (f.mimetype or "").lower()
            fname = (f.filename or "").lower()
            # OCR
            if mime == "application/pdf" or fname.endswith(".pdf"):
                raw_text = ocr_pdf_via_gcs(fb)
            else:
                raw_text = ocr_image_inline(fb)

            # Gemini で回答抽出（CSV 1行）
            model = genai.GenerativeModel("gemini-2.5-flash")
            prompt = f"""
            あなたはOCRで抽出したアンケート回答を整理するAIです。
            次の質問リスト順に、OCR結果から対応する回答だけを取り出し、
            CSV 1行（回答1,回答2,...）で返してください。見つからない箇所は N/A としてください。

            質問リスト:
            {os.linesep.join(header_row)}

            OCR結果:
            {raw_text}
            """
            g = model.generate_content(prompt)
            csv_line = (g.text or "").strip()
            row = [c.strip() for c in csv_line.split(",")]
            # 列数をヘッダに合わせる（不足はN/A、超過は切り捨て）
            if len(row) < len(header_row):
                row += ["N/A"] * (len(header_row) - len(row))
            else:
                row = row[:len(header_row)]
            rows.append(row)

        # 回答追記
        sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range="A2",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()

        return (f"{len(uploaded_files)}件のファイルを処理し、スプレッドシートに書き込みました！", 200, headers)

    except ValueError as e:
        return (str(e), 400, headers)
    except Exception as e:
        print(f"データ抽出中にエラー: {e}")
        return ("データ抽出中にエラーが発生しました。", 500, headers)

@functions_framework.http
def get_sheet_id(request):
    """指定テンプレートに保存してある spreadsheetId を返す（?template=...）"""
    headers = {"Access-Control-Allow-Origin": "*"}
    if request.method == "OPTIONS":
        headers.update({
            "Access-Control-Allow-Methods": "GET",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "3600",
        })
        return ("", 204, headers)

    try:
        uid = verify_token(request)
        template_name = request.args.get("template", "").strip()
        if not template_name:
            raise ValueError("テンプレート名が指定されていません。")

        ref = db.collection("users").document(uid).collection("templates").document(template_name)
        doc = ref.get()
        if not doc.exists:
            raise ValueError(f"テンプレート「{template_name}」が見つかりません。")

        spreadsheet_id = (doc.to_dict() or {}).get("spreadsheetId")
        return (jsonify({"spreadsheetId": spreadsheet_id}), 200, headers)

    except ValueError as e:
        return (str(e), 400, headers)
    except Exception as e:
        print(f"シートID取得中にエラー: {e}")
        return ("シートID取得中にエラーが発生しました。", 500, headers)
