# 手書き文字を画像から読み取ってスプレッドシートに保存するアプリ

## 概要

子供の幼稚園の夏祭り係を担当することになり、各保護者から担当したい希望の役割をアンケートで収集しなければならなくなった。手書きで書かれたアンケート用紙から回答を拾うのが大変だったので、複数人で用紙を撮影し、画像を文字認識で解析し、結果をスプレッドシートに保存するためのアプリを作成しました。
![アプリの画像](https://github.com/user-attachments/assets/6941be76-3630-421e-84e3-156fa730e451 "これはアプリの画像です" )
## 主な機能
- Googleアカウント認証
- ユーザー別設定：Cloud Firestoreを利用し、ユーザーごとに書き込み先のGoogleスプレッドシートIDを保存。
- 複数ファイルアップロード：複数枚の画像を選択してアップロード可能
- 高精度ORR：Google Cloud Vision AIを利用し、画像から高精度に文字を抽出。
- スプレッドシートに自動記録：Google Sheet APIを介して、抽出したテキストを自動で指定のスプレッドシートに追記。
- 自動デプロイ：GitHub Actionsを利用しmainブランチへのプッシュをトリガーにフロントエンドをGitHubPagesへ自動でデプロイ。
- セキュアなキー管理：GitHub Secretsを利用し、APIキーなどの機密情報をソースコードから分離

## 技術スタック

### バックエンド
- Google Cloud Functions(Python 3.12)
- Google Cloud Vision AI: OCR処理
- Gooogle Sheets API: スプレッドシート操作
- Firebase Authentication: ユーザー認証
- Cloud Firestre: ユーザー設定の保存（スプレッドシートのID保存）
- Firebase Admin SDK

### フロントエンド
- HTML （CSSはなし）
- JavaScript
- GitHub Pages: ホスティング
- Firebase

### CI/CD
- GitHub Actions:フロントエンドの自動ビルド＆デプロイ
- Git/GitHub: バージョン管理

## セットアップ手順
1. Google Cloud & Firebase プロジェクトの準備
    1. Google Cloud コンソールにアクセスし、Googleアカウントでログインします
    2. 新しいGoogle Cloud プロジェクトを作成し、課金を有効かします。

2. 以下のAPIを有効化します
    - Cloud Vision API
    - Google Sheets API
    - 注記: この他の必要なAPI（Cloud Functions API, Cloud Build API, Identity Toolkit APIなど）は、後の手順でgcloudコマンドの実行やFirebaseの設定を行った際に、自動で有効化を促されます。

3. Firebaseのセットアップ
    1. Firebaseコンソールにアクセスします
    2. プロジェクトを追加をクリックし、プロジェクト名を入力のプルダウンメニューから、先ほど作成した既存のGoogle Cloudプロジェクトを選択します。
    3. 画面の指示に従い、セットアップを完了させます。

4. 認証情報の準備
    1. Google Cloudコンソールの「APIとサービス」→「認証情報」に移動
    2. サービスアカウントキー
        - 「認証情報を作成」→「サービスアカウント」を選択します 
        - 必要な情報を入力し、役割として「編集者」を選択します。
        - キーの管理画面から「鍵を追加」→「新しい鍵を作成」を選択肢、JSON形式のキーをダウンロードします。
    3. APIキー
        - 「+ 認証情報を作成」→「APIキー」を選択し、新しいキーを作成します。
        - 作成したキーをクリックし、「キーを制限」を設定します。
        - 「アプリケーションの制限」で「HTTPリファラー（ウェブサイト）」を選択し、後でデプロイするサイトのURLを追加します。（例: https://<あなたのユーザー名>.github.io/*）

5. ローカル環境の準備
    1. このリポジトリをgit cloneでPCにコピーします。
    2. プロジェクトのルートフォルダに、ダウンロードしたサービスアカウントキーのJSONファイルをcredentials.jsonという名前で配置します。
    3. .gitignoreファイルにcredentials.jsonという行が含まれていることを確認し、機密情報がGitリポジトリにアップロードされないようにします。
    4. gcloud CLIをインストールし、gcloud initで準備したGoogle Cloudプロジェクトにログインします。

6. バックエンドのデプロイ
    - 以下のコマンドでCloud Functionをデプロイします。[関数名]と[リージョン]は適宜変更してください。
    ```
    gcloud functions deploy [関数名] --gen2 --runtime=python312 --region=[リージョン] --source=. --entry-point=ocr_and_write_sheet --trigger-http --allow-unauthenticated
    ```

7. フロントエンドのデプロイ
    1. GitHubリポジトリの準備:
        新しいGitHubリポジトリを作成し、main.pyやfrontend/フォルダなど、.gitignoreで除外されていない全てのファイルをプッシュします。

    2. GitHub Secretsの設定:
        - リポジトリの「Settings」→「Secrets and variables」→「Actions」で、以下のリポジトリシークレットを設定します。
            - VITE_FIREBASE_API_KEY (手順1-4で作成・制限したAPIキー)
            - VITE_FIREBASE_AUTH_DOMAIN
            - VITE_FIREBASE_PROJECT_ID
            - VITE_FIREBASE_STORAGE_BUCKET
            - VITE_FIREBASE_MESSAGING_SENDER_ID
            - VITE_FIREBASE_APP_ID
            - VITE_CLOUDFUNCTION_URL (手順3でデプロイした関数のURL)
    3. GitHub Actionsの実行:
        - リポジトリの.github/workflows/deploy.ymlファイルが存在することを確認します。
        - mainブランチにgit pushすると、GitHub Actionsが自動で起動し、サイトがGitHub Pagesにデプロイされます。
    4. 最終設定:
        - リポジトリの「Settings」→「Pages」で、公開されたサイトのURLを確認します。
        - Firebaseコンソールの「Authentication」→「Settings」→「承認済みドメイン」に、公開されたサイトのドメイン（例: あなたのユーザー名.github.io）を追加します。