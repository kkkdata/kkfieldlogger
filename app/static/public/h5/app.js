const SETTINGS_KEY = 'kk-field-logger-h5-settings-v1';
const UI_KEY = 'kk-field-logger-h5-ui-v1';
const DB_NAME = 'kk-field-logger-h5';
const DB_VERSION = 1;
const QUEUE_STORE = 'captureQueue';
const DEFAULT_SERVER_URL = window.location.origin || 'https://api1.kkdatasvc.com';
const DEFAULT_SETTINGS = {
  serverUrl: DEFAULT_SERVER_URL,
  employeeId: '',
  projectId: '',
  apiKey: '',
};
const DEFAULT_UI = {
  settingsCollapsed: false,
};

const state = {
  settings: { ...DEFAULT_SETTINGS },
  ui: { ...DEFAULT_UI },
  stream: null,
  db: null,
  syncInProgress: false,
  installPromptEvent: null,
  latestThumbnailUrl: null,
  queuePreviewUrls: new Map(),
  currentUpload: null,
  captureBusy: false,
  cameraStarting: false,
  queueCount: 0,
  lastSavedLabel: 'No media captured yet.',
};

const els = {
  settingsForm: document.getElementById('settingsForm'),
  saveSettingsButton: document.getElementById('saveSettingsButton'),
  employeeIdInput: document.getElementById('employeeIdInput'),
  projectIdInput: document.getElementById('projectIdInput'),
  apiKeyInput: document.getElementById('apiKeyInput'),
  clearSettingsButton: document.getElementById('clearSettingsButton'),
  toggleSettingsButton: document.getElementById('toggleSettingsButton'),
  settingsBody: document.getElementById('settingsBody'),
  settingsSummary: document.getElementById('settingsSummary'),
  storageState: document.getElementById('storageState'),
  workerSummary: document.getElementById('workerSummary'),
  projectSummary: document.getElementById('projectSummary'),
  connectionSummary: document.getElementById('connectionSummary'),
  queueSummaryChip: document.getElementById('queueSummaryChip'),
  startCameraButton: document.getElementById('startCameraButton'),
  stopCameraButton: document.getElementById('stopCameraButton'),
  takePhotoButton: document.getElementById('takePhotoButton'),
  fallbackFileInput: document.getElementById('fallbackFileInput'),
  fallbackCaptureLabel: document.getElementById('fallbackCaptureLabel'),
  cameraPreview: document.getElementById('cameraPreview'),
  cameraStage: document.getElementById('cameraStage'),
  captureCanvas: document.getElementById('captureCanvas'),
  cameraOverlay: document.getElementById('cameraOverlay'),
  captureTip: document.getElementById('captureTip'),
  gpsStatus: document.getElementById('gpsStatus'),
  uploadStatus: document.getElementById('uploadStatus'),
  queueStatus: document.getElementById('queueStatus'),
  captureMessage: document.getElementById('captureMessage'),
  captureThumbnail: document.getElementById('captureThumbnail'),
  queueList: document.getElementById('queueList'),
  syncNowButton: document.getElementById('syncNowButton'),
  clearQueueButton: document.getElementById('clearQueueButton'),
  networkHint: document.getElementById('networkHint'),
  installButton: document.getElementById('installButton'),
  syncProgressWrap: document.getElementById('syncProgressWrap'),
  syncProgressBar: document.getElementById('syncProgressBar'),
  syncProgressLabel: document.getElementById('syncProgressLabel'),
  syncProgressPercent: document.getElementById('syncProgressPercent'),
  toastStack: document.getElementById('toastStack'),
};

bootstrap().catch((error) => {
  console.error(error);
  setUploadStatus(`Startup failed: ${error.message}`);
  showToast(`Startup failed: ${error.message}`, 'error');
});

async function bootstrap() {
  loadSettings();
  loadUiPrefs();
  bindEvents();
  state.db = await openDb();
  await requestPersistentStorage();
  renderSettings();
  applySettingsVisibility();
  renderSummaryChips();
  renderCaptureFeedback();
  renderNetworkStatus();
  await refreshQueueUI();
  await registerServiceWorker();
  scheduleAutoSync();
  updateActionStates();
}

function bindEvents() {
  els.settingsForm.addEventListener('submit', handleSaveSettings);
  els.clearSettingsButton.addEventListener('click', handleClearSettings);
  els.toggleSettingsButton.addEventListener('click', toggleSettingsVisibility);
  els.startCameraButton.addEventListener('click', startCamera);
  els.stopCameraButton.addEventListener('click', () => stopCamera());
  els.takePhotoButton.addEventListener('click', handleTakePhoto);
  els.fallbackFileInput.addEventListener('change', handleFallbackCapture);
  els.syncNowButton.addEventListener('click', () => syncQueue({ manual: true }));
  els.clearQueueButton.addEventListener('click', handleClearQueue);
  els.installButton.addEventListener('click', installPwa);

  window.addEventListener('online', () => {
    renderNetworkStatus();
    showToast('Network is back. Syncing pending captures.', 'info');
    syncQueue();
  });
  window.addEventListener('offline', () => {
    renderNetworkStatus();
    showToast('Offline mode enabled. Captures stay in the local queue.', 'info');
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      renderNetworkStatus();
      syncQueue();
    }
  });
  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    state.installPromptEvent = event;
    els.installButton.classList.remove('hidden');
  });
  window.addEventListener('beforeunload', cleanupPreviewUrls);
}

function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    state.settings = raw ? { ...DEFAULT_SETTINGS, ...JSON.parse(raw) } : { ...DEFAULT_SETTINGS };
  } catch {
    state.settings = { ...DEFAULT_SETTINGS };
  }
}

function saveSettings() {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(state.settings));
}

function loadUiPrefs() {
  try {
    const raw = localStorage.getItem(UI_KEY);
    state.ui = raw ? { ...DEFAULT_UI, ...JSON.parse(raw) } : { ...DEFAULT_UI };
  } catch {
    state.ui = { ...DEFAULT_UI };
  }

  if (!localStorage.getItem(UI_KEY) && hasSavedSettings()) {
    state.ui.settingsCollapsed = window.innerWidth < 940;
    saveUiPrefs();
  }
}

function saveUiPrefs() {
  localStorage.setItem(UI_KEY, JSON.stringify(state.ui));
}

function renderSettings() {
  els.employeeIdInput.value = state.settings.employeeId;
  els.projectIdInput.value = state.settings.projectId;
  els.apiKeyInput.value = state.settings.apiKey;
}

function applySettingsVisibility() {
  els.settingsBody.classList.toggle('hidden', state.ui.settingsCollapsed);
  els.toggleSettingsButton.textContent = state.ui.settingsCollapsed ? 'Show' : 'Hide';
}

function toggleSettingsVisibility() {
  state.ui.settingsCollapsed = !state.ui.settingsCollapsed;
  saveUiPrefs();
  applySettingsVisibility();
}

function collectSettingsFromForm() {
  return {
    serverUrl: DEFAULT_SETTINGS.serverUrl,
    employeeId: els.employeeIdInput.value.trim(),
    projectId: els.projectIdInput.value.trim(),
    apiKey: els.apiKeyInput.value.trim(),
  };
}

function hasSavedSettings() {
  return Boolean(state.settings.employeeId && state.settings.projectId && state.settings.apiKey);
}

function hasValidSettings(settings) {
  return Boolean(settings.employeeId && settings.projectId && settings.apiKey);
}

function validateSettings(settings) {
  if (!settings.employeeId) throw new Error('Employee ID is required.');
  if (!settings.projectId) throw new Error('Project ID is required.');
  if (!settings.apiKey) throw new Error('API key is required.');
}

async function handleSaveSettings(event) {
  event.preventDefault();
  try {
    const settings = collectSettingsFromForm();
    validateSettings(settings);
    state.settings = settings;
    saveSettings();
    if (window.innerWidth < 940) {
      state.ui.settingsCollapsed = true;
      saveUiPrefs();
      applySettingsVisibility();
    }
    renderSummaryChips();
    updateActionStates();
    setUploadStatus('Settings saved on this device.');
    setCaptureTip('Continuous mode is ready. Start the camera and keep capturing.');
    showToast('Worker setup saved on this device.', 'success');
    await syncQueue();
  } catch (error) {
    setUploadStatus(error.message);
    showToast(error.message, 'error');
  }
}

async function handleClearSettings() {
  const confirmed = window.confirm('Clear the stored worker setup from this device?');
  if (!confirmed) {
    return;
  }
  localStorage.removeItem(SETTINGS_KEY);
  state.settings = { ...DEFAULT_SETTINGS };
  renderSettings();
  renderSummaryChips();
  updateActionStates();
  setUploadStatus('Stored settings cleared on this device.');
  setCaptureTip('Enter worker details again before capturing.');
  showToast('Stored worker setup cleared.', 'info');
}

function renderSummaryChips() {
  els.workerSummary.textContent = state.settings.employeeId || 'Not set';
  els.projectSummary.textContent = state.settings.projectId || 'Not set';
  els.queueSummaryChip.textContent = `${state.queueCount} pending`;
  els.settingsSummary.textContent = hasSavedSettings()
    ? `Worker ${state.settings.employeeId} is pinned on this device for project ${state.settings.projectId}.`
    : 'Save once, then come back and keep capturing.';
}

async function requestPersistentStorage() {
  if (!navigator.storage || !navigator.storage.persist) {
    els.storageState.textContent = 'Persistent storage API is not available in this browser.';
    return;
  }

  try {
    const persisted = await navigator.storage.persisted();
    if (persisted) {
      els.storageState.textContent = 'Offline queue storage is already persistent on this device.';
      return;
    }
    const granted = await navigator.storage.persist();
    els.storageState.textContent = granted
      ? 'Offline queue storage has been marked as persistent.'
      : 'Offline queue storage is best-effort; browser cleanup may still remove queued items.';
  } catch (error) {
    els.storageState.textContent = `Storage persistence check failed: ${error.message}`;
  }
}

async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) {
    return;
  }
  try {
    await navigator.serviceWorker.register('./sw.js');
  } catch (error) {
    console.warn('Service worker registration failed', error);
  }
}

function isStandaloneMode() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

function detectPlatform() {
  const userAgent = navigator.userAgent || '';
  if (/iphone|ipad|ipod/i.test(userAgent)) {
    return 'ios';
  }
  if (/android/i.test(userAgent)) {
    return 'android';
  }
  return 'other';
}

function renderInstallExperience() {
  const platform = detectPlatform();
  const standalone = isStandaloneMode();

  els.installSummary.textContent = standalone
    ? 'This H5 shortcut now opens from the phone desktop like an app.'
    : 'Install this H5 tool for faster emergency access in the field.';

  if (standalone) {
    els.installStatus.textContent = 'Added to desktop';
    els.installHelp.textContent = 'Open this shortcut from the phone desktop whenever you need emergency field capture.';
    els.installButton.classList.add('hidden');
    return;
  }

  if (state.installPromptEvent) {
    els.installStatus.textContent = 'Ready to install';
    els.installHelp.textContent = 'Tap "Install to Home Screen", then confirm. The shortcut will appear on the phone desktop.';
    els.installButton.classList.remove('hidden');
    return;
  }

  if (platform === 'ios') {
    els.installStatus.textContent = 'Use Safari share menu';
    els.installHelp.textContent = 'In Safari, tap Share, then choose "Add to Home Screen" to save this tool on the phone desktop.';
    els.installButton.classList.add('hidden');
    return;
  }

  if (platform === 'android') {
    els.installStatus.textContent = 'Use browser menu if needed';
    els.installHelp.textContent = 'If the install button does not appear, open the browser menu and choose "Install app" or "Add to Home screen".';
    els.installButton.classList.add('hidden');
    return;
  }

  els.installStatus.textContent = 'Browser dependent';
  els.installHelp.textContent = 'If supported by your browser, add this page to the home screen from the browser menu.';
  els.installButton.classList.add('hidden');
}

async function installPwa() {
  if (!state.installPromptEvent) {
    renderInstallExperience();
    showToast('Use your browser menu to add this page to the home screen.', 'info');
    return;
  }
  await state.installPromptEvent.prompt();
  const choice = await state.installPromptEvent.userChoice;
  state.installPromptEvent = null;
  els.installButton.classList.add('hidden');
  renderInstallExperience();
  if (choice && choice.outcome === 'accepted') {
    showToast('Install request accepted. Check the phone desktop for the shortcut.', 'success');
  } else {
    showToast('Install cancelled. You can do it later from this page.', 'info');
  }
}

async function startCamera() {
  if (state.cameraStarting || state.captureBusy) {
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    enableFallbackCapture('Camera preview is not supported here. Use fallback capture.');
    showToast('Camera preview is unavailable. Fallback capture is ready.', 'info');
    return;
  }

  state.cameraStarting = true;
  updateActionStates();
  stopCamera({ silent: true });
  setCameraOverlay('Starting rear camera...');
  setCaptureTip('Allow camera permission, then hold the phone steady for a fast capture.');

  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        facingMode: { ideal: 'environment' },
        width: { ideal: 1920 },
        height: { ideal: 1080 },
      },
    });
    state.stream = stream;
    els.cameraPreview.srcObject = stream;
    await els.cameraPreview.play();
    setCameraOverlay('Camera ready. Tap Take Photo.');
    setCaptureTip('Continuous mode is live. Watermarking happens automatically before upload.');
    els.fallbackCaptureLabel.classList.add('hidden');
    showToast('Rear camera is ready.', 'success');
  } catch (error) {
    console.warn(error);
    enableFallbackCapture('Camera permission failed. Use fallback capture.');
    showToast('Camera permission failed. Use fallback capture instead.', 'error');
  } finally {
    state.cameraStarting = false;
    updateActionStates();
  }
}

function stopCamera({ silent = false } = {}) {
  if (!state.stream) {
    setCameraOverlay('Camera idle');
    updateActionStates();
    return;
  }
  for (const track of state.stream.getTracks()) {
    track.stop();
  }
  state.stream = null;
  els.cameraPreview.srcObject = null;
  setCameraOverlay('Camera stopped');
  if (!silent) {
    setCaptureTip('Camera stopped. Start again whenever you are ready for the next capture.');
  }
  updateActionStates();
}

function setCameraOverlay(message) {
  els.cameraOverlay.textContent = message;
}

function enableFallbackCapture(message) {
  setCameraOverlay(message);
  els.fallbackCaptureLabel.classList.remove('hidden');
}

function setCaptureTip(message) {
  els.captureTip.textContent = message;
}
async function handleTakePhoto() {
  if (state.captureBusy) {
    return;
  }

  try {
    const settings = collectSettingsFromForm();
    validateSettings(settings);
    state.settings = settings;
    saveSettings();
    renderSummaryChips();
  } catch (error) {
    setUploadStatus(error.message);
    showToast(error.message, 'error');
    return;
  }

  state.captureBusy = true;
  updateActionStates();
  try {
    if (!state.stream) {
      await startCamera();
      if (!state.stream) {
        return;
      }
    }

    setCameraOverlay('Capturing frame...');
    flashCameraStage();
    const blob = await snapshotVideoFrame();
    await handleCapturedBlob(blob);
    setCameraOverlay('Camera ready. Tap Take Photo.');
  } catch (error) {
    console.error(error);
    setUploadStatus(error.message);
    showToast(error.message, 'error');
    setCameraOverlay('Capture paused. Try again.');
  } finally {
    state.captureBusy = false;
    updateActionStates();
  }
}

async function handleFallbackCapture(event) {
  const file = event.target.files && event.target.files[0];
  if (!file || state.captureBusy) {
    return;
  }

  try {
    const settings = collectSettingsFromForm();
    validateSettings(settings);
    state.settings = settings;
    saveSettings();
    renderSummaryChips();
  } catch (error) {
    setUploadStatus(error.message);
    showToast(error.message, 'error');
    event.target.value = '';
    return;
  }

  state.captureBusy = true;
  updateActionStates();
  try {
    await handleCapturedBlob(file);
  } catch (error) {
    console.error(error);
    setUploadStatus(error.message);
    showToast(error.message, 'error');
  } finally {
    state.captureBusy = false;
    updateActionStates();
    event.target.value = '';
  }
}

function snapshotVideoFrame() {
  return new Promise((resolve, reject) => {
    const width = els.cameraPreview.videoWidth;
    const height = els.cameraPreview.videoHeight;
    if (!width || !height) {
      reject(new Error('Camera frame is not ready yet.'));
      return;
    }
    els.captureCanvas.width = width;
    els.captureCanvas.height = height;
    const ctx = els.captureCanvas.getContext('2d');
    ctx.drawImage(els.cameraPreview, 0, 0, width, height);
    els.captureCanvas.toBlob((blob) => {
      if (!blob) {
        reject(new Error('Failed to capture photo frame.'));
        return;
      }
      resolve(blob);
    }, 'image/jpeg', 0.92);
  });
}

function flashCameraStage() {
  els.cameraStage.classList.remove('capture-flash');
  void els.cameraStage.offsetWidth;
  els.cameraStage.classList.add('capture-flash');
  window.setTimeout(() => {
    els.cameraStage.classList.remove('capture-flash');
  }, 320);
}

function formatTimestampForWatermark(timestampUtc) {
  return new Intl.DateTimeFormat('en-US', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(timestampUtc));
}

function formatCoordinates(latitude, longitude) {
  return `${latitude.toFixed(5)}, ${longitude.toFixed(5)}`;
}

function buildWatermarkLines(queueItem) {
  const projectLabel = queueItem.projectId || 'Not Set';
  const locationLabel = queueItem.location || 'Location unavailable';
  const gpsLabel = queueItem.gpsText || 'Unavailable';
  const timeLabel = formatTimestampForWatermark(queueItem.timestampUtc);
  return [
    'K&K Data Service',
    '',
    `Employee: ${queueItem.employeeId}`,
    `Project: ${projectLabel}`,
    '',
    'Location:',
    locationLabel,
    '',
    'GPS:',
    gpsLabel,
    '',
    'Time:',
    timeLabel,
  ];
}

async function loadBitmapFromBlob(blob) {
  if ('createImageBitmap' in window) {
    return createImageBitmap(blob);
  }

  return new Promise((resolve, reject) => {
    const image = new Image();
    const objectUrl = URL.createObjectURL(blob);
    image.onload = () => {
      URL.revokeObjectURL(objectUrl);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      reject(new Error('Failed to decode captured image.'));
    };
    image.src = objectUrl;
  });
}

async function burnWatermark(blob, queueItem) {
  const bitmap = await loadBitmapFromBlob(blob);
  const width = bitmap.width;
  const height = bitmap.height;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(bitmap, 0, 0, width, height);

  const lines = buildWatermarkLines(queueItem);
  const fontSize = width < 1200 ? 23 : 28;
  const lineHeight = fontSize + 8;
  const horizontalPadding = 18;
  const verticalPadding = 16;
  const boxWidth = Math.min(width * 0.56, 620);
  const boxHeight = lines.length * lineHeight + verticalPadding * 2;
  const boxLeft = 24;
  const boxTop = Math.max(24, height - boxHeight - 24);

  ctx.fillStyle = 'rgba(0, 0, 0, 0.66)';
  drawRoundRect(ctx, boxLeft, boxTop, boxWidth, boxHeight, 16);
  ctx.fill();

  ctx.fillStyle = '#ffffff';
  ctx.font = `600 ${fontSize}px "Segoe UI", "Noto Sans", sans-serif`;
  ctx.textBaseline = 'top';

  let y = boxTop + verticalPadding;
  for (const line of lines) {
    ctx.fillText(line, boxLeft + horizontalPadding, y, boxWidth - horizontalPadding * 2);
    y += lineHeight;
  }

  if (typeof bitmap.close === 'function') {
    bitmap.close();
  }

  return new Promise((resolve, reject) => {
    canvas.toBlob((watermarkedBlob) => {
      if (!watermarkedBlob) {
        reject(new Error('Failed to render watermarked image.'));
        return;
      }
      resolve(watermarkedBlob);
    }, 'image/jpeg', 0.92);
  });
}

function drawRoundRect(ctx, x, y, width, height, radius) {
  const effectiveRadius = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + effectiveRadius, y);
  ctx.arcTo(x + width, y, x + width, y + height, effectiveRadius);
  ctx.arcTo(x + width, y + height, x, y + height, effectiveRadius);
  ctx.arcTo(x, y + height, x, y, effectiveRadius);
  ctx.arcTo(x, y, x + width, y, effectiveRadius);
  ctx.closePath();
}

async function handleCapturedBlob(blob) {
  setUploadStatus('Preparing capture...');
  setGpsStatus('Acquiring precise GPS...');
  setCaptureTip('Hold position for a moment so GPS and watermark details are accurate.');

  const gps = await collectGps();
  const timestampUtc = new Date().toISOString();
  const fileName = `${timestampUtc.replace(/[:.]/g, '').replace(/-/g, '')}_${crypto.randomUUID().slice(0, 8)}.jpg`;
  const queueItem = {
    id: crypto.randomUUID(),
    fileName,
    mimeType: 'image/jpeg',
    blob,
    employeeId: state.settings.employeeId,
    projectId: state.settings.projectId,
    photoType: 'project',
    mediaKind: 'photo',
    timestampUtc,
    gpsText: gps ? `${gps.latitude.toFixed(4)},${gps.longitude.toFixed(4)}` : '',
    gpsLat: gps ? gps.latitude : null,
    gpsLon: gps ? gps.longitude : null,
    location: gps ? `GPS ${formatCoordinates(gps.latitude, gps.longitude)} (${gps.accuracyLabel})` : '',
    accuracyMeters: gps ? gps.accuracyMeters : null,
    heading: '',
    pitch: '',
    roll: '',
    createdAt: Date.now(),
  };

  setUploadStatus('Applying watermark...');
  queueItem.blob = await burnWatermark(queueItem.blob, queueItem);

  await putQueueItem(queueItem);
  state.lastSavedLabel = gps
    ? `Saved locally with GPS ${queueItem.gpsText}`
    : 'Saved locally without GPS';
  setCapturePreview(queueItem.blob, state.lastSavedLabel);
  setCaptureTip('Capture stored locally. The queue will upload automatically when the network is ready.');
  showToast('Capture saved to the local queue.', 'success');
  await refreshQueueUI();
  await syncQueue();
}

async function collectGps() {
  if (!navigator.geolocation) {
    setGpsStatus('GPS unavailable in this browser.');
    return null;
  }

  let slowTimer = null;
  try {
    const position = await new Promise((resolve, reject) => {
      slowTimer = window.setTimeout(() => {
        setGpsStatus('GPS is taking longer than expected. Stay on this screen for better accuracy.');
      }, 3500);

      navigator.geolocation.getCurrentPosition(resolve, reject, {
        enableHighAccuracy: true,
        timeout: 10000,
        maximumAge: 0,
      });
    });

    const { latitude, longitude, accuracy } = position.coords;
    const accuracyMeters = Math.round(accuracy || 0);
    const accuracyLabel = accuracyMeters > 0 ? `+/-${accuracyMeters}m` : 'accuracy unavailable';
    setGpsStatus(`Ready - ${accuracyLabel}`);
    return { latitude, longitude, accuracyMeters, accuracyLabel };
  } catch (error) {
    console.warn(error);
    const proceed = window.confirm('Precise GPS failed. Continue without GPS?');
    if (!proceed) {
      throw new Error('Capture cancelled until precise GPS is available.');
    }
    setGpsStatus('Continuing without precise GPS.');
    showToast('Continuing without precise GPS.', 'info');
    return null;
  } finally {
    if (slowTimer) {
      window.clearTimeout(slowTimer);
    }
  }
}

function setCapturePreview(blob, message) {
  if (state.latestThumbnailUrl) {
    URL.revokeObjectURL(state.latestThumbnailUrl);
  }
  state.latestThumbnailUrl = URL.createObjectURL(blob);
  els.captureThumbnail.src = state.latestThumbnailUrl;
  els.captureThumbnail.classList.remove('hidden');
  els.captureMessage.textContent = message;
}

function renderCaptureFeedback() {
  els.captureMessage.textContent = state.lastSavedLabel;
}

function setGpsStatus(message) {
  els.gpsStatus.textContent = message;
}

function setUploadStatus(message) {
  els.uploadStatus.textContent = message;
}

function renderNetworkStatus() {
  const online = navigator.onLine;
  els.connectionSummary.textContent = online ? 'Online' : 'Offline';
  els.networkHint.textContent = online
    ? 'Network online. Pending captures will sync automatically.'
    : 'Offline mode. Captures will stay in the browser queue until network returns.';
}

function scheduleAutoSync() {
  window.setInterval(() => {
    if (document.visibilityState === 'visible') {
      syncQueue();
    }
  }, 15000);
}
async function syncQueue({ manual = false, targetId = null } = {}) {
  if (state.syncInProgress) {
    if (manual) {
      showToast('Queue sync is already running.', 'info');
    }
    return;
  }
  if (!navigator.onLine) {
    if (manual) {
      setUploadStatus('No network connection. Queue is preserved locally.');
      showToast('No network connection. Queue is preserved locally.', 'error');
    }
    return;
  }
  if (!hasValidSettings(state.settings)) {
    if (manual) {
      setUploadStatus('Complete worker settings before syncing.');
      showToast('Complete worker settings before syncing.', 'error');
    }
    return;
  }

  const queuedItems = (await getQueueItems()).sort((left, right) => left.createdAt - right.createdAt);
  const items = targetId ? queuedItems.filter((item) => item.id === targetId) : queuedItems;

  if (!items.length) {
    if (manual) {
      setUploadStatus('Nothing pending.');
      showToast('Queue is already empty.', 'info');
    }
    await refreshQueueUI();
    return;
  }

  state.syncInProgress = true;
  updateActionStates();

  try {
    for (let index = 0; index < items.length; index += 1) {
      const item = items[index];
      state.currentUpload = {
        id: item.id,
        fileName: item.fileName,
        position: index + 1,
        total: items.length,
        percent: 0,
        stage: 'Uploading',
      };
      renderUploadProgress();
      await refreshQueueUI();
      await uploadQueueItem(item, index + 1, items.length);
      await deleteQueueItem(item.id);
      showToast(`Uploaded ${item.fileName}`, 'success');
      await refreshQueueUI();
    }
    const remainingCount = (await getQueueItems()).length;
    setUploadStatus(
      remainingCount
        ? `${remainingCount} pending capture(s) still need sync.`
        : 'All pending captures are synced.',
    );
  } catch (error) {
    console.error(error);
    setUploadStatus(`Sync paused: ${error.message}`);
    showToast(`Sync paused: ${error.message}`, 'error');
  } finally {
    state.currentUpload = null;
    state.syncInProgress = false;
    renderUploadProgress();
    await refreshQueueUI();
    updateActionStates();
  }
}

async function uploadQueueItem(item, position, total) {
  let slowTimer = null;
  try {
    setUploadStatus(`Uploading ${position}/${total}...`);
    slowTimer = window.setTimeout(() => {
      setUploadStatus('Weak network. Please keep this screen open.');
      showToast('Weak network detected. Keep this screen open while upload finishes.', 'info');
    }, 5000);

    const formData = new FormData();
    formData.append('photo', item.blob, item.fileName);
    formData.append('employee_id', item.employeeId);
    formData.append('project_id', item.projectId);
    formData.append('photo_type', item.photoType);
    formData.append('media_kind', item.mediaKind);
    formData.append('gps', item.gpsText);
    formData.append('gps_lat', item.gpsLat ?? '');
    formData.append('gps_lon', item.gpsLon ?? '');
    formData.append('location', item.location);
    formData.append('heading', item.heading);
    formData.append('pitch', item.pitch);
    formData.append('roll', item.roll);
    formData.append('timestamp', item.timestampUtc);

    await uploadWithProgress(formData, item);
    setUploadStatus(`Uploaded ${item.fileName}`);
  } finally {
    if (slowTimer) {
      window.clearTimeout(slowTimer);
    }
  }
}

function uploadWithProgress(formData, item) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${state.settings.serverUrl}/upload`);
    xhr.timeout = 45000;
    xhr.responseType = 'json';
    xhr.setRequestHeader('X-API-Key', state.settings.apiKey);

    xhr.upload.onprogress = (event) => {
      if (!state.currentUpload || state.currentUpload.id !== item.id) {
        return;
      }
      if (event.lengthComputable && event.total > 0) {
        state.currentUpload.percent = Math.max(1, Math.min(99, Math.round((event.loaded / event.total) * 100)));
      } else {
        state.currentUpload.percent = Math.max(state.currentUpload.percent, 5);
      }
      renderUploadProgress();
    };

    xhr.onload = () => {
      if (!state.currentUpload || state.currentUpload.id !== item.id) {
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        state.currentUpload.percent = 100;
        state.currentUpload.stage = 'Server accepted';
        renderUploadProgress();
        resolve(xhr.response || {});
        return;
      }
      state.currentUpload.percent = Math.min(state.currentUpload.percent, 99);
      state.currentUpload.stage = 'Rejected by server';
      renderUploadProgress();
      reject(new Error(parseXhrError(xhr)));
    };

    xhr.onerror = () => reject(new Error('Network error during upload.'));
    xhr.ontimeout = () => reject(new Error('Upload timed out.'));
    xhr.send(formData);
  });
}

function parseXhrError(xhr) {
  const raw = typeof xhr.responseText === 'string' && xhr.responseText
    ? xhr.responseText
    : JSON.stringify(xhr.response || {});
  const normalized = raw.toLowerCase();
  if (xhr.status === 403 && normalized.includes('employee_id does not match api key')) {
    return 'Upload rejected by the server. Check that the saved Employee ID matches this API key.';
  }
  if (xhr.status === 404 && normalized.includes('unknown project_id')) {
    return 'Upload rejected by the server. Check that the saved Project ID is correct for your company.';
  }
  if (xhr.status === 401 && normalized.includes('invalid api key')) {
    return 'Upload rejected by the server. The API key is invalid or no longer active.';
  }
  return `Upload failed (${xhr.status}): ${raw}`;
}

function renderUploadProgress() {
  if (!state.currentUpload) {
    els.syncProgressWrap.classList.add('hidden');
    els.syncProgressBar.value = 0;
    els.syncProgressLabel.textContent = 'Idle';
    els.syncProgressPercent.textContent = '0%';
    updateQueueProgressUi();
    return;
  }

  els.syncProgressWrap.classList.remove('hidden');
  els.syncProgressBar.value = state.currentUpload.percent;
  els.syncProgressLabel.textContent = `${state.currentUpload.stage} ${state.currentUpload.position}/${state.currentUpload.total} - ${state.currentUpload.fileName}`;
  els.syncProgressPercent.textContent = `${state.currentUpload.percent}%`;
  updateQueueProgressUi();
}

function updateQueueProgressUi() {
  const progressNodes = document.querySelectorAll('[data-upload-progress-id]');
  progressNodes.forEach((node) => {
    const itemId = node.getAttribute('data-upload-progress-id');
    const isActive = state.currentUpload && state.currentUpload.id === itemId;
    node.classList.toggle('hidden', !isActive);
    if (!isActive) {
      return;
    }
    const progress = node.querySelector('progress');
    const label = node.querySelector('[data-upload-progress-label]');
    const percent = node.querySelector('[data-upload-progress-percent]');
    if (progress) {
      progress.value = state.currentUpload.percent;
    }
    if (label) {
      label.textContent = state.currentUpload.stage;
    }
    if (percent) {
      percent.textContent = `${state.currentUpload.percent}%`;
    }
  });
}

async function refreshQueueUI() {
  const items = (await getQueueItems()).sort((left, right) => left.createdAt - right.createdAt);
  state.queueCount = items.length;
  els.queueStatus.textContent = `${items.length} pending`;
  els.queueSummaryChip.textContent = `${items.length} pending`;
  cleanupQueuePreviewUrls(items);
  els.queueList.innerHTML = '';

  if (!items.length) {
    const li = document.createElement('li');
    li.innerHTML = `
      <div class="queue-item-body" style="grid-column: 1 / -1;">
        <div class="queue-item-topline">
          <strong>Queue empty</strong>
          <span class="queue-badge">Ready</span>
        </div>
        <div class="queue-item-detail">
          <div>No local captures are waiting to sync.</div>
          <div>You can keep shooting even under weak network. Successful uploads disappear automatically.</div>
        </div>
      </div>
    `;
    els.queueList.appendChild(li);
    renderSummaryChips();
    updateActionStates();
    return;
  }

  for (const item of items) {
    const li = document.createElement('li');
    const uploading = Boolean(state.currentUpload && state.currentUpload.id === item.id);
    const thumbUrl = getQueuePreviewUrl(item);
    const createdAt = new Date(item.createdAt).toLocaleString();
    const gpsLabel = item.gpsText ? `GPS ${item.gpsText}` : 'No precise GPS stored';
    const sizeLabel = item.blob ? formatBytes(item.blob.size) : 'Size unavailable';
    li.innerHTML = `
      <img class="queue-thumb" src="${thumbUrl}" alt="${item.fileName}" />
      <div class="queue-item-body">
        <div class="queue-item-topline">
          <strong>${escapeHtml(item.fileName)}</strong>
          <span class="queue-badge ${uploading ? 'is-uploading' : ''}">${uploading ? 'Uploading' : 'Pending'}</span>
        </div>
        <div class="queue-item-detail">
          <div>Project ${escapeHtml(item.projectId)} - Employee ${escapeHtml(item.employeeId)}</div>
          <div>${createdAt}</div>
          <div>${gpsLabel}</div>
          <div>${sizeLabel}</div>
        </div>
        <div class="queue-item-progress ${uploading ? '' : 'hidden'}" data-upload-progress-id="${item.id}">
          <div class="queue-item-progress-label">
            <span data-upload-progress-label>${uploading ? state.currentUpload.stage : 'Pending'}</span>
            <strong data-upload-progress-percent>${uploading ? `${state.currentUpload.percent}%` : '0%'}</strong>
          </div>
          <progress max="100" value="${uploading ? state.currentUpload.percent : 0}"></progress>
        </div>
        <div class="queue-item-actions">
          <button class="inline-action-button" type="button" data-action="sync" data-id="${item.id}">Upload Now</button>
          <button class="danger-button" type="button" data-action="delete" data-id="${item.id}">Delete</button>
        </div>
      </div>
    `;

    const syncButton = li.querySelector('[data-action="sync"]');
    const deleteButton = li.querySelector('[data-action="delete"]');
    syncButton.disabled = state.syncInProgress;
    deleteButton.disabled = state.syncInProgress;
    syncButton.addEventListener('click', () => syncQueue({ manual: true, targetId: item.id }));
    deleteButton.addEventListener('click', () => handleDeleteQueueItem(item.id));
    els.queueList.appendChild(li);
  }

  renderSummaryChips();
  updateQueueProgressUi();
  updateActionStates();
}

async function handleDeleteQueueItem(id) {
  if (state.syncInProgress) {
    return;
  }
  const confirmed = window.confirm('Delete this pending capture from the local queue?');
  if (!confirmed) {
    return;
  }
  await deleteQueueItem(id);
  await refreshQueueUI();
  showToast('Pending capture removed from the queue.', 'info');
}

async function handleClearQueue() {
  if (state.syncInProgress) {
    return;
  }
  const items = await getQueueItems();
  if (!items.length) {
    showToast('Queue is already empty.', 'info');
    return;
  }
  const confirmed = window.confirm(`Delete all ${items.length} pending captures from this device?`);
  if (!confirmed) {
    return;
  }
  for (const item of items) {
    await deleteQueueItem(item.id);
  }
  await refreshQueueUI();
  showToast('All pending captures were removed from this device.', 'info');
}

function getQueuePreviewUrl(item) {
  if (!state.queuePreviewUrls.has(item.id)) {
    state.queuePreviewUrls.set(item.id, URL.createObjectURL(item.blob));
  }
  return state.queuePreviewUrls.get(item.id);
}

function cleanupQueuePreviewUrls(items = []) {
  const keepIds = new Set(items.map((item) => item.id));
  for (const [id, url] of state.queuePreviewUrls.entries()) {
    if (!keepIds.has(id)) {
      URL.revokeObjectURL(url);
      state.queuePreviewUrls.delete(id);
    }
  }
}

function cleanupPreviewUrls() {
  if (state.latestThumbnailUrl) {
    URL.revokeObjectURL(state.latestThumbnailUrl);
    state.latestThumbnailUrl = null;
  }
  for (const url of state.queuePreviewUrls.values()) {
    URL.revokeObjectURL(url);
  }
  state.queuePreviewUrls.clear();
}

function updateActionStates() {
  els.startCameraButton.disabled = state.cameraStarting || state.captureBusy || Boolean(state.stream);
  els.stopCameraButton.disabled = state.captureBusy || !state.stream;
  els.takePhotoButton.disabled = state.cameraStarting || state.captureBusy;
  els.fallbackFileInput.disabled = state.captureBusy;
  els.saveSettingsButton.disabled = state.captureBusy || state.syncInProgress;
  els.clearSettingsButton.disabled = state.captureBusy || state.syncInProgress;
  els.syncNowButton.disabled = state.syncInProgress || !state.queueCount || !hasValidSettings(state.settings);
  els.clearQueueButton.disabled = state.syncInProgress || !state.queueCount;

  els.startCameraButton.textContent = state.cameraStarting ? 'Starting...' : 'Start Camera';
  els.takePhotoButton.textContent = state.captureBusy ? 'Saving...' : 'Take Photo';
}

function showToast(message, tone = 'success') {
  const toast = document.createElement('div');
  toast.className = `toast ${tone === 'error' ? 'is-error' : tone === 'info' ? 'is-info' : ''}`;
  toast.textContent = message;
  els.toastStack.appendChild(toast);
  window.setTimeout(() => {
    toast.remove();
  }, 3400);
}

function formatBytes(bytes) {
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const digits = size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[unitIndex]}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onerror = () => reject(request.error);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(QUEUE_STORE)) {
        db.createObjectStore(QUEUE_STORE, { keyPath: 'id' });
      }
    };
    request.onsuccess = () => resolve(request.result);
  });
}

function queueStore(mode = 'readonly') {
  return state.db.transaction(QUEUE_STORE, mode).objectStore(QUEUE_STORE);
}

function putQueueItem(item) {
  return new Promise((resolve, reject) => {
    const request = queueStore('readwrite').put(item);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
}

function getQueueItems() {
  return new Promise((resolve, reject) => {
    const request = queueStore('readonly').getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error);
  });
}

function deleteQueueItem(id) {
  return new Promise((resolve, reject) => {
    const request = queueStore('readwrite').delete(id);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
}





