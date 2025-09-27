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

# ----------------------------------------------------------------
# 司令塔A: テンプレート分析
# ----------------------------------------------------------------
@functions_framework.http
def analyze_survey_template(request):
    """
    単一のアンケート見本画像から質問項目を抽出し、
    テンプレート名とシートIDをFirestoreに保存する。
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

        # --- Geminiによる質問項目の抽出 ---
        model = genai.GenerativeModel("gemini-2.5-flash")
        image_part = {
            "mime_type": uploaded_file.mimetype,
            "data": uploaded_file.read(),
        }

        prompt_for_questions = """
        あなたは、アンケートの構造を分析する専門家です。
        添付されたアンケート画像から、データとして抽出すべき全ての質問項目をリストアップし、
        改行で区切られた単一のテキストとして返してください。
        """

        response = model.generate_content([prompt_for_questions, image_part])
        prompt_items = response.text.strip()

        # --- Firestoreへの保存 ---
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
# 司令塔B: データ抽出実行官
# ----------------------------------------------------------------
@functions_framework.http
def ocr_and_write_sheet(request):
    """
    指定されたテンプレートを使い、複数のアンケートファイルからデータを抽出し、
    Firestoreに保存されているシートIDに書き込む。
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

        template_name = request.form.get("template_name")
        if not template_name:
            raise ValueError("テンプレート名が指定されていません。")

        uploaded_files = request.files.getlist("files")
        if not uploaded_files:
            raise ValueError("ファイルが含まれていません。")

        # --- Firestoreからテンプレート情報を取得 ---
        template_doc_ref = (
            db.collection("users")
            .document(uid)
            .collection("templates")
            .document(template_name)
        )
        template_doc = template_doc_ref.get()
        if not template_doc.exists:
            raise ValueError(f"テンプレート「{template_name}」が見つかりません。")

        template_data = template_doc.to_dict()
        prompt_items = template_data.get("items", "")
        spreadsheet_id = template_data.get("spreadsheetId")

        if not spreadsheet_id:
            raise ValueError("このテンプレートにはシートIDが登録されていません。")

        # --- 各ファイルを処理して、シートに書き込み ---
        # rows_to_insert = []
        # TODO: extract_answers_with_gemini() で回答抽出
        # TODO: write_to_spreadsheet() でシートに書き込み

        return (
            f"{len(uploaded_files)}件のファイルを処理し、シート({spreadsheet_id})に書き込みました！",
            200,
            headers,
        )

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
