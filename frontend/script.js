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
const uploadForm = document.getElementById("upload-form");
const fileInput = document.getElementById("file-input");
const statusMessage = document.getElementById("status-message");
const settingsContainer = document.getElementById("settings-container");
const settingsForm = document.getElementById("settings-form");
const sheetIdInput = document.getElementById("sheet-id-input");
const currentSheetIdElement = document.getElementById("current-sheet-id"); // 新しい要素を取得

// --- グローバル変数 ---
let currentUserIdToken = null;

// --- ログイン状態を監視する ---
onAuthStateChanged(auth, async (user) => {
    if (user) {
        // ログインしている場合
        userInfo.textContent = `ようこそ, ${user.displayName} さん`;
        loginButton.style.display = "none";
        logoutButton.style.display = "block";
        uploadForm.style.display = "block";
        settingsContainer.style.display = "block";

        currentUserIdToken = await user.getIdToken();

        // Firestoreから設定を読み込んで表示
        const userDocRef = doc(db, "users", user.uid);
        const docSnap = await getDoc(userDocRef);
        if (docSnap.exists() && docSnap.data().spreadsheetId) {
            currentSheetIdElement.textContent = docSnap.data().spreadsheetId;
        } else {
            currentSheetIdElement.textContent = "未設定"; // 未設定の場合
        }
    } else {
        // ログアウトしている場合
        userInfo.textContent = "ログインしていません";
        loginButton.style.display = "block";
        logoutButton.style.display = "none";
        uploadForm.style.display = "none";
        settingsContainer.style.display = "none";
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
