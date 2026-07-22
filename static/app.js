const wishButton = document.getElementById('wishButton');
const wishStatus = document.getElementById('wishStatus');
const wishBurst = document.getElementById('wishBurst');

let extractionStarted = false;
const socket = io();

socket.on('connect', () => {
    console.log('Connected to extraction server');
});

socket.on('status_update', (data) => {
    if (data.status === 'capturing') {
        wishStatus.textContent = 'Extracting your gift... please wait.';
        wishButton.disabled = true;
        wishButton.classList.add('loading');
        wishBurst.classList.remove('visible');
    } else if (data.status === 'complete') {
        wishStatus.textContent = 'Your wish has been delivered! ✨';
        wishButton.textContent = 'Wish Delivered';
        wishButton.classList.remove('loading');
        wishBurst.classList.add('visible');
    } else if (data.status === 'error') {
        wishStatus.textContent = 'Something went wrong. Please refresh and try again.';
        wishButton.disabled = false;
        wishButton.classList.remove('loading');
        wishButton.textContent = 'Click Me';
        extractionStarted = false;
        wishBurst.classList.remove('visible');
    }
});

socket.on('log', (data) => {
    console.log('[server]', data.message);
});

wishButton.addEventListener('click', async () => {
    if (extractionStarted) return;
    extractionStarted = true;
    wishButton.disabled = true;
    wishButton.textContent = 'Waiting for your gift...';
    wishStatus.textContent = 'Preparing the extraction...';
    wishButton.classList.add('loading');
    wishBurst.classList.remove('visible');

    try {
        const response = await fetch('/api/extract', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await response.json();
        if (!response.ok) {
            wishStatus.textContent = data.error || 'Failed to start extraction.';
            wishButton.disabled = false;
            wishButton.classList.remove('loading');
            wishButton.textContent = 'Click Me';
            extractionStarted = false;
        }
    } catch (err) {
        wishStatus.textContent = 'Network error. Please refresh and try again.';
        wishButton.disabled = false;
        wishButton.classList.remove('loading');
        wishButton.textContent = 'Click Me';
        extractionStarted = false;
    }
});
