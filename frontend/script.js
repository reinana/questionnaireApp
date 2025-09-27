// Firebase SDK v12.2.1 から必要な関数をインポート
import { initializeApp } from "https://www.gstatic.com/firebasejs/12.2.1/firebase-app.js";
import { getAuth, GoogleAuthProvider, signInWithPopup, signOut, onAuthStateChanged } from "https://www.gstatic.com/firebasejs/12.2.1/firebase-auth.js";
import { getFirestore, collection, getDocs } from "https://www.gstatic.com/firebasejs/12.2.1/firebase-firestore.js";

// config.jsから設定情報を読み込む (GitHub Actionsで自動生成される)
// const firebaseConfig = { ... };
// const analyzeFunctionUrl = '...';
// const processFunctionUrl = '...';
// const getSheetIdFunctionUrl = '...';

// --- Firebaseの初期化 ---
const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app);
const provider = new GoogleAuthProvider();

// --- HTML要素の取得 ---
const loginButton = document.getElementById("login-button");
const logoutButton = document.getElementById("logout-button");
const userInfo = document.getElementById("user-info");
const mainContent = document.getElementById("main-content");
const statusMessage = document.getElementById("status-message");

// フェーズ1
const templateForm = document.getElementById("template-form");
const templateNameInput = document.getElementById("template-name-input");
const templateFileInput = document.getElementById("template-file-input");
const sheetUrlInput = document.getElementById("sheet-url-input");
const templateStatusMessage = document.getElementById("template-status-message");

// フェーズ2
const uploadForm = document.getElementById("upload-form");
const templateSelect = document.getElementById("template-select");
const fileInput = document.getElementById("file-input");
const currentSheetIdElement = document.getElementById("current-sheet-id");

// --- ヘルパー関数 ---

// 都度トークンを取る（失効対策）
async function getIdToken() {
  const user = auth.currentUser;
  if (!user) throw new Error("未ログインです");
  return await user.getIdToken();
}

// UI 初期化ヘルパ
function resetTemplateSelect() {
  templateSelect.innerHTML = '<option value="">テンプレートを選択してください</option>';
  currentSheetIdElement.textContent = "未選択";
}

// テンプレート一覧読み込み
async function loadTemplates() {
  const user = auth.currentUser;
  if (!user) return;
  try {
    resetTemplateSelect();
    const colRef = collection(db, "users", user.uid, "templates");
    const snap = await getDocs(colRef);
    snap.forEach((docSnap) => {
      const opt = document.createElement("option");
      opt.value = docSnap.id;
      opt.textContent = docSnap.id;
      templateSelect.appendChild(opt);
    });
  } catch (e) {
    console.error("テンプレート読み込みエラー:", e);
    statusMessage.textContent = "テンプレートの読み込みに失敗しました。";
  }
}

// テンプレート選択時にシートIDを表示
async function showSheetIdForTemplate(templateName) {
  const user = auth.currentUser;
  if (!user || !templateName) {
    currentSheetIdElement.textContent = "未選択";
    return;
  }

  try {
    const token = await getIdToken();
    const res = await fetch(`${getSheetIdFunctionUrl}?template=${encodeURIComponent(templateName)}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const data = await res.json();
    if (res.ok && data.spreadsheetId) {
      currentSheetIdElement.textContent = data.spreadsheetId;
    } else {
      currentSheetIdElement.textContent = "取得できませんでした";
    }
  } catch (err) {
    console.error("シートID取得エラー:", err);
    currentSheetIdElement.textContent = "エラーが発生しました";
  }
}

// --- 認証処理 ---
loginButton.addEventListener("click", () => {
  signInWithPopup(auth, provider).catch((error) => console.error("ログインエラー:", error));
});

logoutButton.addEventListener("click", () => {
  signOut(auth);
});

onAuthStateChanged(auth, async (user) => {
  if (user) {
    userInfo.textContent = `ようこそ, ${user.displayName} さん`;
    loginButton.style.display = "none";
    logoutButton.style.display = "block";
    mainContent.style.display = "block";
    await loadTemplates();
  } else {
    userInfo.textContent = "ログインしていません";
    loginButton.style.display = "block";
    logoutButton.style.display = "none";
    mainContent.style.display = "none";
    resetTemplateSelect();
  }
});

// --- フォーム処理 ---

// フェーズ1: テンプレート作成
templateForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const submitButton = templateForm.querySelector("button[type=submit]");
  try {
    const token = await getIdToken();
    const templateName = templateNameInput.value.trim();
    const file = templateFileInput.files[0];
    const sheetUrl = sheetUrlInput.value.trim();

    if (!templateName || !file || !sheetUrl) {
      templateStatusMessage.textContent = "テンプレート名・シートURL・ファイルをすべて入力してください。";
      return;
    }

    submitButton?.setAttribute("disabled", "true");
    templateStatusMessage.textContent = `テンプレート「${templateName}」を作成中...`;

    const fd = new FormData();
    fd.append("template_name", templateName);
    fd.append("spreadsheet_url", sheetUrl);
    fd.append("file", file);

    const res = await fetch(analyzeFunctionUrl, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: fd,
    });
    const text = await res.text();
    if (!res.ok) throw new Error(text);
    templateStatusMessage.textContent = text;
    templateForm.reset();
    await loadTemplates();
  } catch (err) {
    console.error("テンプレート作成エラー:", err);
    templateStatusMessage.textContent = `テンプレートの作成に失敗しました：${err instanceof Error ? err.message : err}`;
  } finally {
    submitButton?.removeAttribute("disabled");
  }
});

// フェーズ2: データ抽出
uploadForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const submitButton = uploadForm.querySelector("button[type=submit]");
  try {
    const token = await getIdToken();
    const templateName = templateSelect.value;
    const files = fileInput.files;

    if (!templateName || files.length === 0) {
      statusMessage.textContent = "テンプレートとファイルを選択してください。";
      return;
    }

    submitButton?.setAttribute("disabled", "true");
    statusMessage.textContent = `アップロード中... (${files.length}件)`;

    const fd = new FormData();
    fd.append("template_name", templateName);
    for (let i = 0; i < files.length; i++) fd.append("files", files[i]);

    const res = await fetch(processFunctionUrl, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: fd,
    });
    const text = await res.text();
    if (!res.ok) throw new Error(text);
    statusMessage.textContent = `サーバーからの返事: ${text}`;
    uploadForm.reset();
    resetTemplateSelect();
    await loadTemplates();
  } catch (err) {
    console.error("通信エラー:", err);
    statusMessage.textContent = `通信エラーが発生しました：${err instanceof Error ? err.message : err}`;
  } finally {
    submitButton?.removeAttribute("disabled");
  }
});

// --- イベント ---
// テンプレート選択時にシートIDを表示
templateSelect.addEventListener("change", async () => {
  await showSheetIdForTemplate(templateSelect.value);
});
