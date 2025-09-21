import functions_framework
import datetime
from google.cloud import vision
from googleapiclient.discovery import build
from google.oauth2 import service_account
import firebase_admin
from firebase_admin import auth, firestore

# --- 設定項目 ---
SERVICE_ACCOUNT_FILE = 'credentials.json' 

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# スプレッドシートのID
# SPREADSHEET_ID = '1AUaSBlYGgO7xyAzbCQbtP-PwKZyb89nodS3rc0PvbPI' 

# --- 初期化処理 ---
# Firebase Admin SDKを初期化（Cloud Functions環境では引数なしでOK）
firebase_admin.initialize_app()
# Firestoreクライアントを初期化
db = firestore.client()

# Vision AI
vision_client = vision.ImageAnnotatorClient()



@functions_framework.http
def ocr_and_write_sheet(request):
    """
    HTTP POSTリクエストで画像を受け取り、認証されたユーザーの
    スプレッドシートにOCR結果を書き込む。
    """
    # --- CORS対応ヘッダー ---
    # あらゆる場所からのアクセスを許可する
    headers = {
        'Access-Control-Allow-Origin': '*'
    }
    # ブラウザからの最初の確認リクエスト（preflight/OPTIONS）に対応
    if request.method == 'OPTIONS':
        headers.update({
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': 'Authorization, Content-Type',
            'Access-Control-Max-Age': '3600'
        })
        return ('', 204, headers)

    # --- メソッドとファイルのチェック ---
    if request.method != "POST":
        return ("ファイルをPOSTメソッドで送信してください。", 405, headers)
    
    uploaded_files = request.files.getlist("files")
    if not uploaded_files:
        return ("リクエストにファイルが含まれていません。", 400, headers)

    # --- ユーザー認証 ---
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return ("認証トークンがありません。", 403, headers)
    
    id_token = auth_header.split('Bearer ')[1]
    try:
        decoded_token = auth.verify_id_token(id_token)
        uid = decoded_token['uid']
    except Exception as e:
        return (f"認証トークンの検証に失敗しました: {e}", 403, headers)

    # --- Firestoreからユーザー設定を取得 ---
    try:
        user_doc_ref = db.collection('users').document(uid)
        user_doc = user_doc_ref.get()
        if not user_doc.exists:
            return (f"ユーザー設定が見つかりません。", 404, headers)
        
        spreadsheet_id = user_doc.to_dict().get('spreadsheetId')
        print(f"Retrieved spreadsheet ID: {spreadsheet_id}")
        if not spreadsheet_id:
            return (f"スプレッドシートIDが設定されていません。", 404, headers)
    except Exception as e:
        return (f"Firestoreからの設定取得に失敗しました: {e}", 500, headers)

    # --- メイン処理 (OCRとデータ準備) ---
    rows_to_insert = []
    for uploaded_file in uploaded_files:
        try:
            image_content = uploaded_file.read()
            image = vision.Image(content=image_content)
            response = vision_client.document_text_detection(image=image)
            
            if response.error.message:
                raise Exception(f"Vision APIエラー({uploaded_file.filename}): {response.error.message}")
            
            text = response.full_text_annotation.text
            now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 1行分のデータを作成 (A列:日時, B列:ファイル名, C列:テキスト)
            row_data = [now, uploaded_file.filename, text]
            rows_to_insert.append(row_data)
        except Exception as e:
            # 個々のファイルの処理でエラーが起きても、処理を続行する
            print(f"ファイル処理中にエラー: {e}")
            # エラーがあったことを示す行を追加することもできる
            rows_to_insert.append([now, uploaded_file.filename, f"ERROR: {e}"])
    
    if not rows_to_insert:
        return ("処理できるファイルがありませんでした。", 400, headers)

    # --- スプレッドシートへの書き込み ---
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        sheets_service = build('sheets', 'v4', credentials=creds)
        
        request_body = {
            'values': rows_to_insert
        }
        sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range='シート1!A1',
            valueInputOption='USER_ENTERED',
            body=request_body
        ).execute()
    except Exception as e:
        return (f"スプレッドシートへの書き込みに失敗しました: {e}", 500, headers)

    return (f"{len(rows_to_insert)}件のファイルを処理し、スプレッドシートに書き込みました！", 200, headers)







