import { initializeApp } from "https://www.gstatic.com/firebasejs/12.2.1/firebase-app.js";
import {
    getAuth,
    signInWithPopup,
    GoogleAuthProvider,
    onAuthStateChanged,
    signOut,
} from "https://www.gstatic.com/firebasejs/12.2.1/firebase-auth.js";
import {
    getFirestore,
    doc,
    setDoc,
    getDoc,
} from "https://www.gstatic.com/firebasejs/12.2.1/firebase-firestore.js";

// --- Firebaseの設定 ---configへ移動

// Cloud FunctionのURL configへ移動

// --- Firebaseの初期化 ---
const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app);
const provider = new GoogleAuthProvider();

// --- HTML要素の取得 ---
const loginButton = document.getElementById("login-button");
const logoutButton = document.getElementById("logout-button");
const userInfo = document.getElementById("user-info");
// データ抽出フォーム
const uploadForm = document.getElementById("upload-form");
const fileInput = document.getElementById("file-input");
const templateSelect = document.getElementById("template-select");
const sheetIdInput = document.getElementById("sheet-id-input");

const mainContent = document.getElementById("main-content");
const statusMessage = document.getElementById("status-message");
// const settingsContainer = document.getElementById("settings-container");
// const settingsForm = document.getElementById("settings-form");
// const currentSheetIdElement = document.getElementById("current-sheet-id");
// テンプレート作成フォーム
const templateForm = document.getElementById("template-form");
const templateNameInput = document.getElementById("template-name-input");
const templateFileInput = document.getElementById("template-file-input");
const templateStatusMessage = document.getElementById(
    "template-status-message"
);

// --- グローバル変数 ---
let currentUserIdToken = null;

// --- ログイン状態を監視する ---
onAuthStateChanged(auth, async (user) => {
    if (user) {
        // ログインしている場合
        userInfo.textContent = `ようこそ, ${user.displayName} さん`;
        loginButton.style.display = "none";
        logoutButton.style.display = "block";
        // uploadForm.style.display = "block";
        // settingsContainer.style.display = "block";
        mainContent.style.display = "block";
        currentUserIdToken = await user.getIdToken();

        // // Firestoreから設定を読み込んで表示
        // const userDocRef = doc(db, "users", user.uid);
        // const docSnap = await getDoc(userDocRef);
        // if (docSnap.exists() && docSnap.data().spreadsheetId) {
        //     currentSheetIdElement.textContent = docSnap.data().spreadsheetId;
        // } else {
        //     currentSheetIdElement.textContent = "未設定"; // 未設定の場合
        // }

        // Firestoreからテンプレート一覧を読み込む
        const templatesCollectionRef = collection(
            db,
            "users",
            user.uid,
            "templates"
        );
        const snapshot = await getDocs(templatesCollectionRef);
        templateSelect.innerHTML =
            '<option value="">テンプレートを選択してください</option>'; // 初期化
        snapshot.forEach((doc) => {
            const option = document.createElement("option");
            option.value = doc.id;
            option.textContent = doc.id;
            templateSelect.appendChild(option);
        });
    } else {
        // ログアウトしている場合
        userInfo.textContent = "ログインしていません";
        loginButton.style.display = "block";
        logoutButton.style.display = "none";
        mainContent.style.display = "none";
        // uploadForm.style.display = "none";
        // settingsContainer.style.display = "none";
        currentUserIdToken = null;
    }
});

// --- ログイン処理 ---
loginButton.addEventListener("click", () => {
    signInWithPopup(auth, provider).catch((error) => {
        console.error("ログインエラー:", error);
        statusMessage.textContent = `ログインエラー: ${error.message}`;
    });
});

// --- ログアウト処理 ---
logoutButton.addEventListener("click", () => {
    signOut(auth);
});

// --- テンプレート作成フォームの処理 ---
templateForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!currentUserIdToken) {
        templateStatusMessage.textContent = "ログインしてください。";
        return;
    }
    const templateName = templateNameInput.value;
    const file = templateFileInput.files[0];
    if (!templateName || !file) {
        templateStatusMessage.textContent =
            "テンプレート名とファイルを選択してください。";
        return;
    }
    templateStatusMessage.textContent = `テンプレート「${templateName}」を作成中...`;

    const formData = new FormData();
    formData.append("template_name", templateName);
    formData.append("file", file);

    try {
        const response = await fetch(analyzeFunctionUrl, {
            method: "POST",
            headers: { Authorization: `Bearer ${currentUserIdToken}` },
            body: formData,
        });
        const result = await response.text();
        templateStatusMessage.textContent = result;
        if (response.ok) {
            // 成功したらテンプレート一覧を再読み込み
            onAuthStateChanged(auth, auth.currentUser);
        }
    } catch (error) {
        console.error("テンプレート作成エラー:", error);
        templateStatusMessage.textContent =
            "テンプレートの作成に失敗しました。";
    }
});

// --- データ抽出フォームの処理 ---
uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!currentUserIdToken) {
        statusMessage.textContent = "ログインしてください。";
        return;
    }
    const templateName = templateSelect.value;
    const spreadsheetId = sheetIdInput.value;
    const files = fileInput.files;

    if (!templateName || !spreadsheetId || files.length === 0) {
        statusMessage.textContent =
            "テンプレート、シートID、ファイルすべてを選択・入力してください。";
        return;
    }

    statusMessage.textContent = `アップロード中... (${files.length}件)`;
    const formData = new FormData();
    formData.append("template_name", templateName);
    formData.append("spreadsheet_id", spreadsheetId);

    for (let i = 0; i < files.length; i++) {
        formData.append("files", files[i]);
    }

    try {
        const response = await fetch(processFunctionUrl, {
            method: "POST",
            headers: { Authorization: `Bearer ${currentUserIdToken}` },
            body: formData,
        });
        const result = await response.text();
        statusMessage.textContent = `サーバーからの返事: ${result}`;
    } catch (error) {
        console.error("通信エラー:", error);
        statusMessage.textContent = "通信エラーが発生しました。";
    }
});

// --- 設定フォームの保存処理 ---
settingsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const user = auth.currentUser;
    if (user) {
        let spreadsheetId = sheetIdInput.value.trim(); // .trim()で前後の空白を削除

        // 入力された値がURL形式かチェック
        if (
            spreadsheetId.startsWith("https://docs.google.com/spreadsheets/d/")
        ) {
            // URLからID部分だけを抜き出す正規表現
            const match = spreadsheetId.match(/\/d\/(.*?)\//);
            if (match && match[1]) {
                spreadsheetId = match[1]; // 抜き出したIDを代入
                sheetIdInput.value = spreadsheetId; // 入力欄もIDだけの表示に更新
                console.log("抽出されたスプレッドシートID:", spreadsheetId);
            }
        }

        if (!spreadsheetId) {
            statusMessage.textContent =
                "スプレッドシートIDを入力してください。";
            return;
        }
        const userDocRef = doc(db, "users", user.uid);
        try {
            await setDoc(userDocRef, { spreadsheetId: spreadsheetId });
            statusMessage.textContent = "設定を保存しました。";

            // 保存に成功したら、表示エリアも更新
            currentSheetIdElement.textContent = spreadsheetId;
            sheetIdInput.value = ""; // 入力欄はクリアする
        } catch (error) {
            console.error("設定の保存エラー:", error);
            statusMessage.textContent = "設定の保存に失敗しました。";
        }
    }
});

// --- アップロードフォームの処理 ---
uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    if (!currentUserIdToken) {
        statusMessage.textContent =
            "ログイン情報が取得できませんでした。ページを再読み込みしてください。";
        return;
    }

    const files = fileInput.files;
    if (files.length === 0) {
        statusMessage.textContent = "ファイルを選択してください。";
        return;
    }

    statusMessage.textContent = `アップロード中... (${files.length}件)`;

    const formData = new FormData();
    for (let i = 0; i < files.length; i++) {
        formData.append("files", files[i]);
    }

    const promptItems = document.getElementById("prompt-items").value;
    formData.append("prompt_items", promptItems);

    try {
        const response = await fetch(cloudFunctionUrl, {
            method: "POST",
            headers: {
                Authorization: `Bearer ${currentUserIdToken}`,
            },
            body: formData,
        });

        const result = await response.text();

        if (response.ok) {
            statusMessage.textContent = `成功！サーバーからの返事: ${result}`;
        } else {
            statusMessage.textContent = `エラー: ${result}`;
        }
    } catch (error) {
        console.error("通信エラー:", error);
        statusMessage.textContent =
            "通信エラーが発生しました。コンソールを確認してください。";
    }
});
