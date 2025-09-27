# ----------------------------------------------------------------
# 1. 必要なライブラリをすべてインポート
# ----------------------------------------------------------------
import os
import re
import functions_framework
import firebase_admin
from firebase_admin import auth, firestore
import google.generativeai as genai
from flask import jsonify
from google.cloud import vision


# ----------------------------------------------------------------
# 2. 初期化と設定
# ----------------------------------------------------------------
# Firebase Admin SDKを初期化
try:
    firebase_admin.get_app()
except ValueError:
    firebase_admin.initialize_app()

db = firestore.client()

# Gemini APIキーを環境変数から設定
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
# Google Cloud Visionクライアントを初期化
vision_client = vision.ImageAnnotatorClient()


# ----------------------------------------------------------------
# ユーティリティ関数
# ----------------------------------------------------------------
def extract_spreadsheet_id(url: str) -> str:
    """
    GoogleスプレッドシートのURLからIDを抽出する。
    """
    pattern = r"/d/([a-zA-Z0-9-_]+)"
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    return url.strip()

def verify_token(request):
    """
    Authorization ヘッダから ID トークンを検証し、uid を返す。
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise ValueError("認証トークンがありません。")
    id_token = auth_header.split("Bearer ")[1]
    decoded_token = auth.verify_id_token(id_token)
    return decoded_token["uid"]

def extract_text_with_vision(file_data: bytes, mime_type: str) -> str:
    """
    Google Cloud Vision API を使って画像/PDFからテキストを抽出する。
    """
    image = vision.Image(content=file_data)
    response = vision_client.document_text_detection(image=image)
    
    if response.error.message:
        raise RuntimeError(f"Vision API error: {response.error.message}")

    return response.full_text_annotation.text.strip()


# ----------------------------------------------------------------
# 司令塔A: テンプレート分析 (Vision + Gemini)
# ----------------------------------------------------------------
@functions_framework.http
def analyze_survey_template(request):
    """
    単一のアンケート見本画像/PDFから質問項目を抽出し、
    テンプレートとしてFirestoreに保存する。
    Vision APIでOCR → Geminiで項目抽出。
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
        # --- 認証 ---
        uid = verify_token(request)

        # --- 入力チェック ---
        template_name = request.form.get("template_name")
        if not template_name:
            raise ValueError("テンプレート名が指定されていません。")

        spreadsheet_url = request.form.get("spreadsheet_url")
        if not spreadsheet_url:
            raise ValueError("スプレッドシートURLが指定されていません。")

        spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)

        uploaded_file = request.files.get("file")
        if not uploaded_file:
            raise ValueError("ファイルが含まれていません。")

        file_bytes = uploaded_file.read()

        # --- Step1: VisionでOCR ---
        vision_client = vision.ImageAnnotatorClient()
        image = vision.Image(content=file_bytes)
        response = vision_client.document_text_detection(image=image)
        if response.error.message:
            raise RuntimeError(f"Vision API error: {response.error.message}")

        raw_text = response.full_text_annotation.text.strip()
        if not raw_text:
            raise ValueError("OCRでテキストを抽出できませんでした。")

        # --- Step2: Geminiで質問項目を抽出 ---
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt_for_questions = """
        あなたはアンケート設計を分析する専門家です。
        以下のOCRテキストから、回答欄に相当する質問項目のみを抽出してください。
        出力は改行区切りのリストとしてください。
        """
        gemini_response = model.generate_content([prompt_for_questions, raw_text])
        prompt_items = gemini_response.text.strip()

        # --- Firestoreへ保存 ---
        template_doc_ref = (
            db.collection("users")
            .document(uid)
            .collection("templates")
            .document(template_name)
        )
        template_doc_ref.set({
            "items": prompt_items,
            "spreadsheetId": spreadsheet_id,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })

        return (f"テンプレート「{template_name}」を作成しました。", 200, headers)

    except ValueError as e:
        return (str(e), 400, headers)
    except Exception as e:
        print(f"テンプレート作成中にエラー: {e}")
        return ("テンプレート作成中にエラーが発生しました。", 500, headers)

# ----------------------------------------------------------------
# 司令塔B: データ抽出実行官 (Vision + Gemini, ヘッダ行あり)
# ----------------------------------------------------------------
@functions_framework.http
def ocr_and_write_sheet(request):
    """
    指定されたテンプレートを使い、複数のアンケート画像/PDFから
    回答を抽出し、指定されたGoogleスプレッドシートに書き込む。
    VisionでOCR → Geminiで回答抽出。
    1行目に質問リストをヘッダとして出力する。
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
        # --- 認証 ---
        uid = verify_token(request)

        # --- 入力チェック ---
        template_name = request.form.get("template_name")
        if not template_name:
            raise ValueError("テンプレート名が指定されていません。")

        uploaded_files = request.files.getlist("files")
        if not uploaded_files:
            raise ValueError("ファイルが含まれていません。")

        # --- Firestoreからテンプレート取得 ---
        template_doc_ref = (
            db.collection("users").document(uid).collection("templates").document(template_name)
        )
        template_doc = template_doc_ref.get()
        if not template_doc.exists:
            raise ValueError(f"テンプレート「{template_name}」が見つかりません。")

        template_data = template_doc.to_dict()
        prompt_items = template_data["items"].splitlines()
        spreadsheet_id = template_data.get("spreadsheetId")
        if not spreadsheet_id:
            raise ValueError("テンプレートに紐付いたスプレッドシートIDが存在しません。")

        # --- Google Sheets API 初期化 ---
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        sheets_service = build("sheets", "v4", credentials=creds)

        # --- まずヘッダ行を書き込み（質問リスト） ---
        header_row = [q.strip() for q in prompt_items if q.strip()]
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="A1",
            valueInputOption="RAW",
            body={"values": [header_row]},
        ).execute()

        # --- Vision + Gemini で各ファイルを処理 ---
        rows_to_insert = []
        for file in uploaded_files:
            file_bytes = file.read()

            # Step1: Vision OCR
            vision_client = vision.ImageAnnotatorClient()
            image = vision.Image(content=file_bytes)
            response = vision_client.document_text_detection(image=image)
            if response.error.message:
                raise RuntimeError(f"Vision API error: {response.error.message}")

            raw_text = response.full_text_annotation.text.strip()

            # Step2: Geminiで回答抽出
            model = genai.GenerativeModel("gemini-2.5-flash")
            prompt_for_answers = f"""
            あなたはOCRで抽出したアンケート回答を整理するAIです。
            以下の質問リストに基づき、OCR結果から対応する回答を抜き出してください。
            回答が見つからない場合は「N/A」としてください。
            出力は質問リストと同じ順序で、CSV形式（回答1,回答2,...）としてください。

            質問リスト:
            {os.linesep.join(header_row)}

            OCR結果:
            {raw_text}
            """
            gemini_response = model.generate_content(prompt_for_answers)
            answers_csv = gemini_response.text.strip()

            # CSV文字列 → リスト化
            row = [ans.strip() for ans in answers_csv.split(",")]
            rows_to_insert.append(row)

        # --- 回答を2行目以降に追記 ---
        body = {"values": rows_to_insert}
        sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="A2",
            valueInputOption="RAW",
            body=body,
        ).execute()

        return (f"{len(uploaded_files)}件のファイルを処理し、スプレッドシートに書き込みました！", 200, headers)

    except ValueError as e:
        return (str(e), 400, headers)
    except Exception as e:
        print(f"データ抽出中にエラー: {e}")
        return ("データ抽出中にエラーが発生しました。", 500, headers)


# ----------------------------------------------------------------
# 司令塔C: シートID確認用API
# ----------------------------------------------------------------
@functions_framework.http
def get_sheet_id(request):
    """
    指定されたテンプレートに紐づくシートIDを返す。
    クエリパラメータ: ?template=テンプレート名
    """
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

        template_name = request.args.get("template")
        if not template_name:
            raise ValueError("テンプレート名が指定されていません。")

        template_doc_ref = (
            db.collection("users")
            .document(uid)
            .collection("templates")
            .document(template_name)
        )
        template_doc = template_doc_ref.get()
        if not template_doc.exists:
            raise ValueError(f"テンプレート「{template_name}」が見つかりません。")

        data = template_doc.to_dict()
        spreadsheet_id = data.get("spreadsheetId")

        return (jsonify({"spreadsheetId": spreadsheet_id}), 200, headers)

    except ValueError as e:
        return (str(e), 400, headers)
    except Exception as e:
        print(f"シートID取得中にエラー: {e}")
        return ("シートID取得中にエラーが発生しました。", 500, headers)
