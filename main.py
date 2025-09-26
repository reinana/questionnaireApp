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
firebase_admin.initialize_app()
db = firestore.client()

# Gemini APIキーを環境変数から設定
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# Google Sheets APIに関する設定
SERVICE_ACCOUNT_FILE = 'credentials.json'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# ----------------------------------------------------------------
# 3. ヘルパー関数（個別の作業を担当する小さな関数）
# ----------------------------------------------------------------

def validate_request_and_get_config(request):
    """リクエストを検証し、認証を行い、ユーザー設定を取得する。"""
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        raise ValueError("認証トークンがありません。")
    
    id_token = auth_header.split('Bearer ')[1]
    decoded_token = auth.verify_id_token(id_token)
    uid = decoded_token['uid']

    uploaded_files = request.files.getlist("files")
    if not uploaded_files:
        raise ValueError("ファイルが含まれていません。")

    user_doc_ref = db.collection('users').document(uid)
    user_doc = user_doc_ref.get()
    if not user_doc.exists or not user_doc.to_dict().get('spreadsheetId'):
        raise ValueError("スプレッドシートIDが設定されていません。")
    
    spreadsheet_id = user_doc.to_dict()['spreadsheetId']
    
    return uploaded_files, spreadsheet_id

def extract_questions_with_gemini(image_part):
    """Geminiを使って画像から質問項目をリストアップする。"""
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt_for_questions = """
    あなたは、アンケートの構造を分析する専門家です。
    添付されたアンケート画像から、データとして抽出すべき全ての質問項目をリストアップし、
    改行で区切られた単一のテキストとして返してください。
    """
    
    response = model.generate_content([prompt_for_questions, image_part])
    return response.text.strip()

def extract_answers_with_gemini(image_part, prompt_items):
    """抽出された質問項目リストを元に、画像から回答を抽出する。"""
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt_for_extraction = f"""
    あなたは、アンケートのデータ入力を行う専門家です。
    添付された画像を解析し、以下の項目について回答を特定し、JSON形式で結果を抽出してください。
    もし項目が見つからない、または回答がない場合は、値を "未回答" としてください。

    抽出する項目:
    {prompt_items}
    """
    
    response = model.generate_content([prompt_for_extraction, image_part])
    json_text = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(json_text)

def write_to_spreadsheet(spreadsheet_id, rows_to_insert):
    """指定されたスプレッドシートに複数行のデータを書き込む。"""
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    sheets_service = build('sheets', 'v4', credentials=creds)
    
    request_body = {'values': rows_to_insert}
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range='シート1!A1',
        valueInputOption='USER_ENTERED',
        body=request_body
    ).execute()

# ----------------------------------------------------------------
# 4. メイン関数（司令塔）
# ----------------------------------------------------------------

@functions_framework.http
def ocr_and_write_sheet(request):
    """HTTPリクエストを受け付け、各処理を順番に呼び出す司令塔。"""
    headers = {'Access-Control-Allow-Origin': '*'}
    if request.method == 'OPTIONS':
        headers.update({
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': 'Authorization, Content-Type',
            'Access-Control-Max-Age': '3600'
        })
        return ('', 204, headers)

    try:
        # --- 受付担当を呼び出す ---
        # リクエストが正当かチェックし、ファイルと設定を取得
        uploaded_files, spreadsheet_id = validate_request_and_get_config(request)

        # --- AI分析担当を呼び出す (ステップ1) ---
        # 複数ファイルがアップロードされても、最初の1枚だけを使ってアンケートの構造を分析させる
        first_page_content = uploaded_files[0].read()
        first_image_part = {"mime_type": uploaded_files[0].mimetype, "data": first_page_content}
        # 質問項目のリストを取得
        prompt_items = extract_questions_with_gemini(first_image_part)

        # --- AIデータ入力担当を呼び出す (ステップ2) ---
        # 書き込むための行データを準備
        rows_to_insert = []
        # 全てのファイルを1枚ずつループ処理
        for uploaded_file in uploaded_files:
            now = datetime.datetime.now().strftime('%Y-%M-%d %H:%M:%S')
            try:
                # ファイルを再度読み込む必要があるため、seek(0)で読み取り位置を先頭に戻す
                uploaded_file.seek(0)
                image_content = uploaded_file.read()
                image_part = {"mime_type": uploaded_file.mimetype, "data": image_content}

                # ステップ1で取得した質問リストを元に、回答を抽出させる
                extracted_data = extract_answers_with_gemini(image_part, prompt_items)

                # 抽出したデータをスプレッドシートの行形式に整える
                # ※この部分は、抽出した項目に合わせて調整が必要です
                row_data = [
                    now,
                    uploaded_file.filename,
                    # extracted_data (辞書) から、prompt_items (キーのリスト) を使って値を取り出す
                    # (この部分はより洗練させる必要があります)
                ]

            except Exception as e:
                print(f"ファイル処理中にエラー: {e}")
                row_data = [now, uploaded_file.filename, f"ERROR: {e}"]
            
            rows_to_insert.append(row_data)

        # --- 書記担当を呼び出す ---
        if rows_to_insert:
            write_to_spreadsheet(spreadsheet_id, rows_to_insert)

        return (f"{len(rows_to_insert)}件のファイルを処理し、書き込みました！", 200, headers)

    except ValueError as e:
        # 想定内のリクエストエラー
        return (str(e), 400, headers)
    except Exception as e:
        # 想定外のサーバー内部エラー
        print(f"予期せぬエラーが発生しました: {e}")
        return (f"予期せぬエラーが発生しました。", 500, headers)