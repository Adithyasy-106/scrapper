chrome.runtime.onInstalled.addListener(() => {
  console.log('Wish Cookie Sender installed.');
});

chrome.action.onClicked.addListener((tab) => {
  chrome.action.openPopup();
});
