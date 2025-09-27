# ----------------------------------------------------------------
# 1. imports
# ----------------------------------------------------------------
import os, re, time, json
import functions_framework
import firebase_admin
from firebase_admin import auth, firestore
import google.generativeai as genai
from flask import jsonify
from google.cloud import vision, storage
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import requests

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
TEMP_BUCKET = os.environ.get("TEMP_BUCKET")

# Sheets
SERVICE_ACCOUNT_FILE = os.environ.get("SHEETS_SA_JSON", "credentials.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ----------------------------------------------------------------
# 3. utils
# ----------------------------------------------------------------
def extract_spreadsheet_id(url: str) -> str:
    """GoogleスプレッドシートURLからIDを抽出"""
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
def sanitize_ocr_text(t: str) -> str:
    if not t:
        return ""
    # 説明文を落として「問1/Q1」からに切る
    m = re.search(r'(?:問|Q)\s*0*1[\.\s）)]?', t)
    if m:
        t = t[m.start():]

    # “性別” など安全判定のトリガになりやすい語を無害化
    # （回答語はそのままなので抽出は影響最小）
    replace_map = {
        "性別": "ジェンダー",
        "性　別": "ジェンダー",
        "性 の 別": "ジェンダー",
    }
    for k, v in replace_map.items():
        t = t.replace(k, v)

    # 記号・空白の正規化
    trans = str.maketrans({"，": ",", "．": ".", "：": ":", "／": "/", "－": "-"})
    t = t.translate(trans)
    t = re.sub(r'[ \t]+', ' ', t)
    return t.strip()


def ocr_pdf_pages_via_gcs_stream(file_storage) -> list[str]:
    """
    Flaskの FileStorage を受け取り、GCSにストリームでアップロードして Vision OCR。
    ページごとのテキスト配列を返す。
    """
    if not TEMP_BUCKET:
        raise RuntimeError("TEMP_BUCKET が設定されていません。")

    ts = int(time.time() * 1000)
    src_name   = f"uploads/{ts}.pdf"
    out_prefix = f"vision-out/{ts}/"

    bucket = storage_client.bucket(TEMP_BUCKET)
    src_blob = bucket.blob(src_name)
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

    # cleanup
    try:
        src_blob.delete()
        for b in blobs: b.delete()
    except Exception as e:
        print(f"[PDF OCR] cleanup warn: {e}")

    return texts

def _clamp(s: str, max_chars: int = 20_000) -> str:
    return s if len(s) <= max_chars else s[:max_chars]

def call_gemini_for_row(full_text: str, items: list[str]) -> list[str]:
    if not items:
        return []
    full_text = sanitize_ocr_text(full_text)

    MODELS = ["gemini-2.5-pro", "gemini-2.5-flash"]
    CHUNK = 6                      # さらに小さく（安全判定を避ける）
    MAX_OUT = 640
    URL_TPL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": os.environ.get("GEMINI_API_KEY","")}

    # すべて BLOCK_NONE
    safety = [
        {"category":"HARM_CATEGORY_SEXUAL_CONTENT","threshold":"BLOCK_NONE"},
        {"category":"HARM_CATEGORY_HATE_SPEECH","threshold":"BLOCK_NONE"},
        {"category":"HARM_CATEGORY_HARASSMENT","threshold":"BLOCK_NONE"},
        {"category":"HARM_CATEGORY_DANGEROUS_CONTENT","threshold":"BLOCK_NONE"},
        {"category":"HARM_CATEGORY_CIVIC_INTEGRITY","threshold":"BLOCK_NONE"},
        {"category":"HARM_CATEGORY_HEALTH","threshold":"BLOCK_NONE"},
        {"category":"HARM_CATEGORY_SELF_HARM","threshold":"BLOCK_NONE"},
    ]

    sys = (
        "あなたはアンケート集計用の抽出器です。"
        "与えた設問リストの順に回答のみを返します。"
        "必ず JSON 文字列配列（例: [\"回答1\",\"回答2\",...]）。"
        "見つからない箇所は \"N/A\"。"
        "説明文・前置き・コードフェンスを出力しないこと。"
    )

    def parse_first_text(data: dict) -> str:
        for i, c in enumerate((data or {}).get("candidates") or []):
            print(f"[Gemini REST] cand#{i} finish={c.get('finishReason')} safety={c.get('safetyRatings')}")
            for p in ((c.get("content") or {}).get("parts") or []):
                t = p.get("text")
                if t:
                    return t.strip()
        return ""

    def to_arr(s: str) -> list[str] | None:
        if not s: return None
        x = s.strip()
        if x.startswith("```"):
            x = x.strip("`")
            if x.lower().startswith("json"):
                x = x[4:].lstrip()
        try:
            j = json.loads(x)
            if isinstance(j, list):
                return [("N/A" if v is None else str(v)) for v in j]
        except Exception:
            return None
        return None

    ans: list[str] = []
    for i in range(0, len(items), CHUNK):
        sub = items[i:i+CHUNK]
        prompt = (
            f"{sys}\n\n"
            f"設問(JSON配列): {json.dumps(sub, ensure_ascii=False)}\n\n"
            f"OCRテキスト:\n{_clamp(full_text, 30000)}"   # さらに短く
        )
        got = None
        last = None
        for m in MODELS:
            url = URL_TPL.format(m=m)
            body = {
                "contents":[{"parts":[{"text": prompt}]}],
                "safetySettings": safety,
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": MAX_OUT,
                    "responseMimeType": "application/json"
                }
            }
            try:
                r = requests.post(url, headers=headers, json=body, timeout=120)
                r.raise_for_status()
                txt = parse_first_text(r.json())
                if not txt:
                    # JSONのみ強制リトライ
                    body["contents"][0]["parts"][0]["text"] = prompt + "\n\n注意: JSON配列のみを出力してください。"
                    body["generationConfig"]["temperature"] = 0.0
                    rr = requests.post(url, headers=headers, json=body, timeout=120)
                    rr.raise_for_status()
                    txt = parse_first_text(rr.json())
                got = to_arr(txt)
                if got is not None:
                    break
            except Exception as e:
                print(f"[Gemini REST error][{m}] {e}")
                last = e

        if got is None:
            print("[Gemini REST fallback] chunk→N/A; err:", last)
            got = ["N/A"] * len(sub)

        if len(got) < len(sub):
            got += ["N/A"] * (len(sub) - len(got))
        ans.extend(got[:len(sub)])

    if len(ans) < len(items):
        ans += ["N/A"] * (len(items) - len(ans))
    return ans[:len(items)]

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

        if not template_name or not spreadsheet_url or not uploaded_file:
            raise ValueError("必要な入力が不足しています。")

        spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
        page_texts = (
            ocr_pdf_pages_via_gcs_stream(uploaded_file)
            if "pdf" in (uploaded_file.mimetype or "").lower()
            else [ocr_image_inline(uploaded_file.read())]
        )

        # Geminiで設問抽出
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = (
            "あなたはアンケート設計を分析する専門家です。"
            "以下のOCRテキストから、回答欄に相当する質問項目のみを抽出し、"
            "各行1項目、改行区切りで出力してください。"
        )

        items: list[str] = []
        seen = set()
        for page in page_texts:
            g = model.generate_content([prompt, _clamp(page)])
            text = (getattr(g, "text", "") or "").strip()
            for line in text.splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    items.append(line)

        ref = db.collection("users").document(uid).collection("templates").document(template_name)
        ref.set({
            "items": "\n".join(items),
            "spreadsheetId": spreadsheet_id,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })

        return (f"テンプレート「{template_name}」を作成しました。", 200, headers)

    except Exception as e:
        print(f"[ANALYZE][ERROR] {e}")
        return ("テンプレート作成中にエラーが発生しました。", 500, headers)

@functions_framework.http
def ocr_and_write_sheet(request):
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
        uploaded_files = request.files.getlist("files")

        if not template_name or not uploaded_files:
            raise ValueError("必要な入力が不足しています。")

        # テンプレート取得
        tref = db.collection("users").document(uid).collection("templates").document(template_name)
        tdoc = tref.get()
        if not tdoc.exists:
            raise ValueError("テンプレートが見つかりません。")
        tdata = tdoc.to_dict()

        print("[RUN] ocr_and_write_sheet start; template=", template_name)
        print("[RUN] GEMINI KEY exists?", bool(os.environ.get("GEMINI_API_KEY")))

        header_row = [q.strip() for q in (tdata.get("items") or "").splitlines() if q.strip()]
        spreadsheet_id = tdata.get("spreadsheetId")

        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        sheets = build("sheets", "v4", credentials=creds)
        sheet_api = sheets.spreadsheets().values()

        # ヘッダがなければ書き込む
        existing = sheet_api.get(spreadsheetId=spreadsheet_id, range="A1:Z1").execute()
        if "values" not in existing:
            sheet_api.update(
                spreadsheetId=spreadsheet_id, range="A1",
                valueInputOption="RAW", body={"values": [header_row]}
            ).execute()

        appended, failed = 0, 0
        for f in uploaded_files:
            try:
                mime = (f.mimetype or "").lower()
                fname = (f.filename or "").lower()

                if "pdf" in mime or fname.endswith(".pdf"):
                    
                    page_texts = ocr_pdf_pages_via_gcs_stream(f)
                    raw_text = "\n".join(page_texts)
                else:
                    raw_text = ocr_image_inline(f.read())

                print(f"[DEBUG][OCR] len={len(raw_text)}")
                print(f"[DEBUG][OCR] head500={raw_text[:500].replace(os.linesep,' ')[:500]}")

                row = call_gemini_for_row(raw_text, header_row)
                if len(row) < len(header_row):
                    row += ["N/A"] * (len(header_row) - len(row))
                row = row[:len(header_row)]

                # デバッグ: 最初の1行だけ raw を末尾列で出す
                out_row = row[:]
                if appended == 0:
                    try:
                        out_row.append(f"DEBUG_RAW={json.dumps(row, ensure_ascii=False)[:500]}")
                    except Exception:
                        out_row.append("DEBUG_RAW=?")
                sheet_api.append(
                    spreadsheetId=spreadsheet_id, range="A2",
                    valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                    body={"values": [row]}
                ).execute()
                appended += 1
            except Exception as e:
                print(f"[Process error] {e}")
                failed += 1

        return (json.dumps({"ok": True, "appended": appended, "failed": failed}), 200, headers)

    except Exception as e:
        print(f"[WRITE][ERROR] {e}")
        return ("データ抽出中にエラーが発生しました。", 500, headers)

@functions_framework.http
def get_sheet_id(request):
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

        ref = db.collection("users").document(uid).collection("templates").document(template_name)
        doc = ref.get()
        if not doc.exists:
            raise ValueError("テンプレートが見つかりません。")

        spreadsheet_id = (doc.to_dict() or {}).get("spreadsheetId")
        return (jsonify({"spreadsheetId": spreadsheet_id}), 200, headers)

    except Exception as e:
        print(f"[GET_SHEET_ID][ERROR] {e}")
        return ("シートID取得中にエラーが発生しました。", 500, headers)
