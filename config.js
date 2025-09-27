const firebaseConfig = {
  apiKey: 'AIzaSyAxc2dEEFuiK8qV2J8ebiXQ7TZRkpVrVm0',
  authDomain: 'questionnaire-app-472614.firebaseapp.com',
  projectId: 'questionnaire-app-472614',
  storageBucket: 'questionnaire-app-472614.firebasestorage.app',
  messagingSenderId: '678983264323',
  appId: '1:678983264323:web:ee07b9fed5f7c14f400cf6',
};
const cloudFunctionUrl = 'https://asia-northeast1-questionnaire-app-472614.cloudfunctions.net/questionnaire-ocr';
const analyzeFunctionUrl = 'https://asia-northeast1-questionnaire-app-472614.cloudfunctions.net/analyze-survey-template';
const processFunctionUrl = 'https://asia-northeast1-questionnaire-app-472614.cloudfunctions.net/ocr-and-write-sheet';
const getSheetIdUrl      = 'https://asia-northeast1-questionnaire-app-472614.cloudfunctions.net/get-sheet-id';
