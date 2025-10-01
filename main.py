# ----------------------------------------------------------------
# 1. imports
# ----------------------------------------------------------------
import os, re, time, json, datetime
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

# Env
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TEMP_BUCKET = os.environ.get("TEMP_BUCKET", "")
GOOGLE_SHEETS_SA_JSON = os.environ.get("GOOGLE_SHEETS_SA_JSON", "")

# ---- Gemini init (APIキー経路を優先) ----
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
if GEMINI_API_KEY:
    # SDKは GOOGLE_API_KEY も見るので両方に設定しておく
    os.environ["GOOGLE_API_KEY"] = GEMINI_API_KEY
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    print("[BOOT] Gemini API key: OK")
else:
    # ここでは落とさない。リクエスト処理時に 500 を返す
    print("[BOOT][WARN] GEMINI_API_KEY/GOOGLE_API_KEY が未設定。Gemini呼び出し時にエラーになります。")


# SDKは GOOGLE_API_KEY も見ます。念のため両方に入れておく
os.environ["GOOGLE_API_KEY"] = GEMINI_API_KEY

import google.generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)

print(f"[BOOT] Gemini API key detected? {'yes' if bool(GEMINI_API_KEY) else 'no'}")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Vision / Storage
# （警告を減らしたいなら transport="rest" も可）
vision_client = vision.ImageAnnotatorClient()
storage_client = storage.Client()

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
        raise RuntimeError(f"Vision API error: {resp.error.message}")
    return (resp.full_text_annotation.text or "").strip()

def ocr_pdf_via_gcs_stream(file_storage) -> str:
    """PDF→GCS→Vision 非同期OCR。全文テキストを結合して返す。"""
    if not TEMP_BUCKET:
        raise RuntimeError("TEMP_BUCKET が設定されていません。")

    ts = int(time.time() * 1000)
    src_name   = f"uploads/{ts}.pdf"
    out_prefix = f"vision-out/{ts}/"  # 末尾スラッシュ重要

    bucket = storage_client.bucket(TEMP_BUCKET)
    src_blob = bucket.blob(src_name)
    # read() せずにストリームアップロード（メモリ節約）
    src_blob.upload_from_file(file_storage.stream, content_type="application/pdf", rewind=True)
    print(f"[PDF OCR] uploaded: gs://{TEMP_BUCKET}/{src_name}")

    gcs_src = vision.GcsSource(uri=f"gs://{TEMP_BUCKET}/{src_name}")
    gcs_dst = vision.GcsDestination(uri=f"gs://{TEMP_BUCKET}/{out_prefix}")

    feature = vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)
    input_config  = vision.InputConfig(gcs_source=gcs_src, mime_type="application/pdf")
    output_config = vision.OutputConfig(gcs_destination=gcs_dst)

    req = vision.AsyncAnnotateFileRequest(
        features=[feature], input_config=input_config, output_config=output_config
    )
    op = vision_client.async_batch_annotate_files(requests=[req])
    op.result(timeout=600)
    print(f"[PDF OCR] async finished. output prefix: gs://{TEMP_BUCKET}/{out_prefix}")

    blobs = list(storage_client.list_blobs(TEMP_BUCKET, prefix=out_prefix))
    json_blobs = [b for b in blobs if b.name.endswith(".json")]
    if not json_blobs:
        raise RuntimeError(f"OCR結果(JSON)が見つかりません: gs://{TEMP_BUCKET}/{out_prefix}")

    texts = []
    for b in json_blobs:
        data = json.loads(b.download_as_bytes().decode("utf-8"))
        for r in data.get("responses", []):
            txt = r.get("fullTextAnnotation", {}).get("text", "")
            if txt:
                texts.append(txt)

    # cleanup（best-effort）
    try:
        src_blob.delete()
        for b in blobs:
            b.delete()
    except Exception as e:
        print(f"[PDF OCR] cleanup warn: {e}")

    return "\n".join(texts).strip()

def _clamp(s: str, max_chars: int = 30_000) -> str:
    return s if len(s) <= max_chars else s[:max_chars]

def get_sheets_service():
    """SecretのJSON文字列から認証情報を作る（ファイル不要）。"""
    if not GOOGLE_SHEETS_SA_JSON:
        raise RuntimeError("GOOGLE_SHEETS_SA_JSON が未設定です。")
    info = json.loads(GOOGLE_SHEETS_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)

# --- Gemini ラッパ（設問抽出／回答抽出） ---
def gemini_extract_questions(ocr_text: str) -> list[str]:
    """OCRテキストから設問リスト（1行1項目）を抽出。"""
    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = (
        "あなたはアンケート設計を分析する専門家です。"
        "以下のOCRテキストから、回答欄に相当する質問項目のみを抽出し、"
        "各行1項目、改行区切りのプレーンテキストで出力してください。"
        "設問番号(例: 問1, Q2, 1.)が付いていれば残してください。"
        "\n\nOCRテキスト:\n" + _clamp(ocr_text)
    )
    out = model.generate_content(prompt)
    text = (getattr(out, "text", "") or "").strip()
    return [ln.strip() for ln in text.splitlines() if ln.strip()]

def gemini_extract_answers_as_array(ocr_text: str, items: list[str]) -> list[str]:
    """
    設問ごとに近傍テキスト（contexts）を用意し、LLMには
    ・contexts（{qid: テキスト}）
    ・items（[{qid,label}]）
    を渡して JSON 配列で回答を強制。
    """
    if not items:
        return []

    contexts = build_contexts_for_items(ocr_text, items)
    items_meta = [{"qid": (extract_qid_from_item(it) or ""), "label": it} for it in items]

    sys = (
        "あなたはアンケート集計の抽出器です。"
        "与えた『質問配列』の順に、各質問の回答のみを返してください。"
        "選択式では番号や丸印（○・●・✓・□等）や '1 男性 2 女性' のような凡例を読み取り、"
        "該当する『選択肢ラベル』を文字列で返してください。"
        "数値項目は単位を除いた数値（例: 年齢→'74', 身長→'166.5'）を返してください。"
        "見つからない箇所は必ず \"N/A\"。"
        "出力は必ず JSON の『文字列配列』のみ（余計な説明やキーは禁止）。"
    )

    prompt = (
        f"{sys}\n\n"
        f"【コンテキスト辞書(JSON; qid→周辺OCRテキスト)】\n"
        f"{json.dumps(contexts, ensure_ascii=False)}\n\n"
        f"【質問配列(JSON; この順に回答を返す)】\n"
        f"{json.dumps(items_meta, ensure_ascii=False)}\n\n"
        f"注意:\n"
        f"- 出力は質問配列と同じ順序の JSON 文字列配列のみ。\n"
        f"- 回答は可能な限り原文のラベルを用い、番号だけの場合は対応するラベル名に置換してください。\n"
        f"- マトリクスは該当セルのラベル（例: '週3回' や 'とても満足' 等）を返してください。\n"
    )

    model = genai.GenerativeModel("gemini-2.5-flash")
    try:
        out = model.generate_content(
            prompt,
            generation_config={
                "response_mime_type": "application/json",
                "temperature": 0.0,
                "max_output_tokens": 2048,
            },
        )
        txt = (getattr(out, "text", "") or "").strip()
        arr = json.loads(txt)
        if not isinstance(arr, list):
            raise ValueError("not list")
    except Exception as e:
        print(f"[LLM parse warn] {e} -> fallback Pro")
        try:
            out = genai.GenerativeModel("gemini-2.5-pro").generate_content(
                prompt,
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": 0.0,
                    "max_output_tokens": 2048,
                },
            )
            txt = (getattr(out, "text", "") or "").strip()
            arr = json.loads(txt)
            if not isinstance(arr, list):
                raise ValueError("not list")
        except Exception as ee:
            print(f"[LLM parse fail] {ee}")
            arr = ["N/A"] * len(items)

    if len(arr) < len(items):
        arr += ["N/A"] * (len(items) - len(arr))
    return [("N/A" if v is None else str(v)) for v in arr[:len(items)]]


# === 設問ID抽出 & OCR分割（追加） ===================================
_QID_BLOCK_PAT = re.compile(r'(?:^|\n)\s*(?:付問\s*)?(?:問|Q)\s*([0-9]+(?:-[0-9]+)?)\s*[\.．）)]?')

def extract_qid_from_item(item_line: str) -> str | None:
    """ヘッダ行（テンプレの1行）から設問ID（例: '37', '39-1'）を抜く"""
    if not item_line: 
        return None
    m = re.search(r'(?:付問\s*)?(?:問|Q)\s*([0-9]+(?:-[0-9]+)?)', item_line)
    return m.group(1) if m else None

def segment_text_by_qid(full_text: str) -> dict[str, str]:
    """
    OCR全文を「問XX / QXX」ごとのブロックに分割。
    戻り値: {'37': '...問37の周辺テキスト...', '39-1': '...'}
    """
    if not full_text:
        return {}
    matches = list(_QID_BLOCK_PAT.finditer(full_text))
    blocks: dict[str, str] = {}
    if not matches:
        return blocks
    for i, m in enumerate(matches):
        qid = m.group(1)                    # '37' or '39-1'
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(full_text)
        blocks[qid] = full_text[start:end].strip()
    return blocks

def build_contexts_for_items(full_text: str, items: list[str]) -> dict[str, str]:
    """
    設問ごとの抽出用コンテキスト（周辺OCRテキスト）を作る。
    1設問あたり最大 ~2000文字にクランプしてサイズ抑制。
    """
    blocks = segment_text_by_qid(full_text)
    ctx: dict[str, str] = {}
    for it in items:
        qid = extract_qid_from_item(it)
        c = ""
        if qid and qid in blocks:
            c = blocks[qid]
        elif qid and '-' in qid:
            # 付問は親設問のブロックも効くことがある（例: '39-1' → '39'）
            base = qid.split('-', 1)[0]
            if base in blocks:
                c = blocks[base]
        # キーワード近傍フォールバック（番号が取れない or ブロック無い）
        if not c:
            # 漢字/かな/英数の2文字以上をキー候補に
            words = re.findall(r'[一-龯ぁ-んァ-ヶa-zA-Z0-9]{2,}', it)
            found = None
            for w in words[:3]:
                p = full_text.find(w)
                if p != -1:
                    s = max(0, p - 300)
                    e = min(len(full_text), p + 1200)
                    found = full_text[s:e]
                    break
            c = found or full_text[:1200]
        ctx[qid or f"idx{len(ctx)}"] = c[:2000]
    return ctx
# ==================================================================

# ----------------------------------------------------------------
# 4. Functions
# ----------------------------------------------------------------
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
        spreadsheet_url = (request.form.get("spreadsheet_url") or "").strip()
        uploaded_file = request.files.get("file")

        if not template_name:
            raise ValueError("テンプレート名が指定されていません。")
        if not spreadsheet_url:
            raise ValueError("スプレッドシートURLが指定されていません。")
        if not uploaded_file:
            raise ValueError("ファイルが含まれていません。")

        spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)

        mime  = (uploaded_file.mimetype or "").lower()
        fname = (uploaded_file.filename or "")
        if "pdf" in mime or fname.lower().endswith(".pdf"):
            raw_text = ocr_pdf_via_gcs_stream(uploaded_file)
        else:
            raw_text = ocr_image_inline(uploaded_file.read())
        
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            return ("サーバのGemini APIキーが未設定です。管理者に連絡してください。", 500, headers)

        items = gemini_extract_questions(raw_text)

        ref = db.collection("users").document(uid).collection("templates").document(template_name)
        ref.set({
            "items": "\n".join(items),
            "spreadsheetId": spreadsheet_id,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })

        return (f"テンプレート「{template_name}」を作成しました。", 200, headers)

    except ValueError as e:
        return (str(e), 400, headers)
    except Exception as e:
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

        uploaded_files = request.files.getlist("files")
        if not uploaded_files:
            raise ValueError("ファイルが含まれていません。")
        
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            return ("サーバのGemini APIキーが未設定です。管理者に連絡してください。", 500, headers)
        
        # テンプレート取得（ここから spreadsheetId も取得）
        tref = db.collection("users").document(uid).collection("templates").document(template_name)
        tdoc = tref.get()
        if not tdoc.exists:
            raise ValueError(f"テンプレート「{template_name}」が見つかりません。")
        tdata = tdoc.to_dict()

        header_row = [q.strip() for q in (tdata.get("items") or "").splitlines() if q.strip()]
        spreadsheet_id = tdata.get("spreadsheetId")
        if not spreadsheet_id:
            raise ValueError("テンプレートに紐付いたスプレッドシートIDが存在しません。")

        sheets = get_sheets_service()
        sheet_api = sheets.spreadsheets().values()

        # 1行目にヘッダ（常に上書き）
        sheet_api.update(
            spreadsheetId=spreadsheet_id, range="A1",
            valueInputOption="RAW", body={"values": [header_row]}
        ).execute()

        rows = []
        for f in uploaded_files:
            try:
                mime = (f.mimetype or "").lower()
                fname = (f.filename or "")
                if "pdf" in mime or fname.lower().endswith(".pdf"):
                    raw_text = ocr_pdf_via_gcs_stream(f)
                else:
                    raw_text = ocr_image_inline(f.read())

                row = gemini_extract_answers_as_array(raw_text, header_row)
                # 列数合わせ（安全側）
                if len(row) < len(header_row):
                    row += ["N/A"] * (len(header_row) - len(row))
                row = row[:len(header_row)]
                rows.append(row)
            except Exception as e:
                print(f"[Process error] {e}")
                rows.append(["N/A"] * len(header_row))

        if rows:
            sheet_api.append(
                spreadsheetId=spreadsheet_id, range="A2",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": rows}
            ).execute()

        return (f"{len(rows)}件のファイルを処理し、スプレッドシートに書き込みました！", 200, headers)

    except ValueError as e:
        return (str(e), 400, headers)
    except Exception as e:
        print(f"[WRITE][ERROR] {e}")
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
        print(f"[GET_SHEET_ID][ERROR] {e}")
        return ("シートID取得中にエラーが発生しました。", 500, headers)
