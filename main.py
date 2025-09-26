# ----------------------------------------------------------------
# 1. 必要なライブラリをすべてインポート
# ----------------------------------------------------------------
import os
import json
import datetime
import functions_framework
import firebase_admin
from firebase_admin import auth, firestore
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build

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

# Google Sheets APIに関する設定
SERVICE_ACCOUNT_FILE = 'credentials.json'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# ----------------------------------------------------------------
# 司令塔A: テンプレート分析
# ----------------------------------------------------------------
@functions_framework.http
def analyze_survey_template(request):
    """
    単一のアンケート見本画像から質問項目を抽出し、
    テンプレートとしてFirestoreに保存する。
    """
    headers = {'Access-Control-Allow-Origin': '*'}
    if request.method == 'OPTIONS':
        headers.update({
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': 'Authorization, Content-Type',
            'Access-Control-Max-Age': '3600'
        })
        return ('', 204, headers)

    try:
        # --- 認証とリクエスト内容の検証 ---
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            raise ValueError("認証トークンがありません。")
        
        id_token = auth_header.split('Bearer ')[1]
        decoded_token = auth.verify_id_token(id_token)
        uid = decoded_token['uid']

        template_name = request.form.get("template_name")
        if not template_name:
            raise ValueError("テンプレート名が指定されていません。")
            
        uploaded_file = request.files.get("file")
        if not uploaded_file:
            raise ValueError("ファイルが含まれていません。")

        # --- Geminiによる質問項目の抽出 ---
        model = genai.GenerativeModel('gemini-2.5-flash')
        image_part = {"mime_type": uploaded_file.mimetype, "data": uploaded_file.read()}
        
        prompt_for_questions = """
        あなたは、アンケートの構造を分析する専門家です。
        添付されたアンケート画像から、データとして抽出すべき全ての質問項目をリストアップし、
        改行で区切られた単一のテキストとして返してください。
        """
        
        response = model.generate_content([prompt_for_questions, image_part])
        prompt_items = response.text.strip()
        
        # --- Firestoreへのテンプレート保存 ---
        template_doc_ref = db.collection('users').document(uid).collection('templates').document(template_name)
        template_doc_ref.set({
            'items': prompt_items,
            'createdAt': firestore.SERVER_TIMESTAMP
        })
        
        return (f"テンプレート「{template_name}」を作成しました。", 200, headers)

    except ValueError as e:
        return (str(e), 400, headers)
    except Exception as e:
        print(f"テンプレート作成中にエラー: {e}")
        return (f"テンプレート作成中にエラーが発生しました。", 500, headers)
    

# ----------------------------------------------------------------
# 司令塔B: データ抽出実行官
# ----------------------------------------------------------------
@functions_framework.http
def ocr_and_write_sheet(request):
    """
    指定されたテンプレートを使い、複数のアンケートファイルからデータを抽出し、
    指定されたスプレッドシートに結果を書き込む。
    """
    headers = {'Access-Control-Allow-Origin': '*'}
    if request.method == 'OPTIONS':
        headers.update({
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': 'Authorization, Content-Type',
            'Access-Control-Max-Age': '3600'
        })
        return ('', 204, headers)

    try:
        # --- 認証とリクエスト内容の検証 ---
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            raise ValueError("認証トークンがありません。")
        
        id_token = auth_header.split('Bearer ')[1]
        decoded_token = auth.verify_id_token(id_token)
        uid = decoded_token['uid']

        template_name = request.form.get("template_name")
        if not template_name:
            raise ValueError("テンプレート名が指定されていません。")

        spreadsheet_id = request.form.get("spreadsheet_id")
        if not spreadsheet_id:
            raise ValueError("スプレッドシートIDが指定されていません。")

        uploaded_files = request.files.getlist("files")
        if not uploaded_files:
            raise ValueError("ファイルが含まれていません。")

        # --- Firestoreからテンプレート（質問項目リスト）を取得 ---
        template_doc_ref = db.collection('users').document(uid).collection('templates').document(template_name)
        template_doc = template_doc_ref.get()
        if not template_doc.exists:
            raise ValueError(f"テンプレート「{template_name}」が見つかりません。")
        prompt_items = template_doc.to_dict()['items']
        
        # --- 各ファイルを処理して、書き込むデータを作成 ---
        rows_to_insert = []
        # (ここで、以前定義した extract_answers_with_gemini や write_to_spreadsheet といった
        # ヘルパー関数を呼び出すロジックを組み立てます)

        return (f"{len(uploaded_files)}件のファイルを処理し、書き込みました！", 200, headers)

    except ValueError as e:
        return (str(e), 400, headers)
    except Exception as e:
        print(f"データ抽出中にエラー: {e}")
        return (f"データ抽出中にエラーが発生しました。", 500, headers)