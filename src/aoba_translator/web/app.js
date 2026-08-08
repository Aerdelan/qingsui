const state = {
  jobs: new Map(),
  system: null,
  polling: null,
  setupTerminalSeen: null,
  assistantMessages: [],
  assistantBusy: false,
};

const elements = {
  dropZone: document.querySelector('#dropZone'),
  fileInput: document.querySelector('#fileInput'),
  setupPanel: document.querySelector('#setupPanel'),
  setupButton: document.querySelector('#setupButton'),
  setupMessage: document.querySelector('#setupMessage'),
  refreshButton: document.querySelector('#refreshButton'),
  healthRing: document.querySelector('#healthRing'),
  healthValue: document.querySelector('#healthValue'),
  translationStatus: document.querySelector('#translationStatus'),
  ocrStatus: document.querySelector('#ocrStatus'),
  deviceStatus: document.querySelector('#deviceStatus'),
  accessStatus: document.querySelector('#accessStatus'),
  outputStatus: document.querySelector('#outputStatus'),
  runtimeHint: document.querySelector('#runtimeHint'),
  uploadLimit: document.querySelector('#uploadLimit'),
  jobList: document.querySelector('#jobList'),
  emptyJobs: document.querySelector('#emptyJobs'),
  jobCounter: document.querySelector('#jobCounter'),
  toast: document.querySelector('#toast'),
  assistantPanel: document.querySelector('#assistantPanel'),
  assistantToggle: document.querySelector('#assistantToggle'),
  assistantClose: document.querySelector('#assistantClose'),
  assistantClear: document.querySelector('#assistantClear'),
  assistantMessages: document.querySelector('#assistantMessages'),
  assistantInput: document.querySelector('#assistantInput'),
  assistantPaste: document.querySelector('#assistantPaste'),
  assistantSend: document.querySelector('#assistantSend'),
  assistantModel: document.querySelector('#assistantModel'),
};

function showToast(message, type = 'normal') {
  elements.toast.textContent = message;
  elements.toast.className = `toast show ${type === 'error' ? 'error' : ''}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    elements.toast.className = 'toast';
  }, 3600);
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  let data = {};
  try { data = await response.json(); } catch (_) { /* empty response */ }
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function badge(element, ready, successText, failureText) {
  element.textContent = ready ? successText : failureText;
  element.className = ready ? 'good' : 'warn';
}

function renderSystem(system) {
  state.system = system;
  const { models, environment, config } = system;
  const dependenciesReady = Object.values(models.dependencies || {}).every(Boolean);
  const readyParts = [models.translation_ready, models.ocr_ready, dependenciesReady].filter(Boolean).length;
  const health = Math.round((readyParts / 3) * 100);
  elements.healthRing.style.setProperty('--health', `${health}%`);
  elements.healthValue.textContent = `${health}%`;
  badge(elements.translationStatus, models.translation_ready, '已就绪', '待初始化');
  badge(elements.ocrStatus, models.ocr_ready, '已就绪', '待初始化');
  const device = environment.cuda_available ? (environment.gpu || 'CUDA GPU') : 'CPU';
  elements.deviceStatus.textContent = device;
  elements.deviceStatus.className = environment.cuda_available ? 'good' : '';
  const accessUrl = config.access_urls?.find(url => !url.includes('127.0.0.1')) || config.access_urls?.[0] || '不可用';
  elements.accessStatus.textContent = accessUrl.replace(/^https?:\/\//, '');
  elements.accessStatus.title = accessUrl;
  elements.accessStatus.className = accessUrl === '不可用' ? 'warn' : 'good';
  elements.outputStatus.textContent = config.output_dir.split(/[\\/]/).slice(-2).join('/');
  elements.outputStatus.title = config.output_dir;
  elements.uploadLimit.textContent = `${config.limits.max_upload_mb} MB`;

  const hint = environment.recommendations?.[0]
    || (models.ready ? '所有模型均在本地可用，可以开始翻译。' : '完成初始化后即可离线处理原稿。');
  elements.runtimeHint.textContent = hint;
  const shouldSetup = !models.ready;
  elements.setupPanel.classList.toggle('hidden', !shouldSetup);
  if (models.setup_error) elements.setupMessage.textContent = models.setup_error;
  elements.setupButton.disabled = false;
  elements.setupButton.textContent = models.setup_error ? '重新初始化' : '开始初始化';
}

async function loadSystem() {
  elements.refreshButton.disabled = true;
  try {
    renderSystem(await fetchJson('/api/system'));
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    elements.refreshButton.disabled = false;
  }
}

function jobLabel(job) {
  if (job.kind === 'setup') return 'SET';
  if (job.detected_type === 'manga') return '漫';
  if (job.detected_type === 'novel') return '文';
  return '译';
}

function renderJobs() {
  const jobs = [...state.jobs.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
  elements.jobCounter.textContent = `${jobs.length} 个任务`;
  elements.emptyJobs.hidden = jobs.length > 0;
  elements.jobList.replaceChildren(...jobs.map(job => {
    const item = document.createElement('article');
    item.className = 'job-item';

    const type = document.createElement('div');
    type.className = `job-type ${job.kind === 'setup' ? 'setup' : ''}`;
    type.textContent = jobLabel(job);

    const main = document.createElement('div');
    main.className = 'job-main';
    const titleRow = document.createElement('div');
    titleRow.className = 'job-title-row';
    const title = document.createElement('span');
    title.className = 'job-title';
    title.textContent = job.filename;
    title.title = job.filename;
    const stage = document.createElement('span');
    stage.className = 'job-stage';
    stage.textContent = job.status === 'failed' ? '失败' : `${job.progress}% · ${job.stage}`;
    titleRow.append(title, stage);
    const progress = document.createElement('div');
    progress.className = 'job-progress';
    const bar = document.createElement('i');
    bar.style.width = `${job.progress}%`;
    if (job.status === 'failed') bar.style.background = '#c97968';
    progress.append(bar);
    main.append(titleRow, progress);
    if (job.message) {
      const message = document.createElement('p');
      message.className = 'job-message';
      message.textContent = job.message;
      main.append(message);
    }

    const action = document.createElement('div');
    action.className = 'job-action';
    if (job.download_url) {
      const link = document.createElement('a');
      link.href = job.download_url;
      link.textContent = '下载产物';
      action.append(link);
    } else {
      const status = document.createElement('span');
      status.textContent = ({ queued: '排队中', running: '处理中', failed: '请检查日志' })[job.status] || '完成';
      action.append(status);
      if (job.status === 'failed') {
        const assistantButton = document.createElement('button');
        assistantButton.className = 'job-assistant-button';
        assistantButton.type = 'button';
        assistantButton.textContent = '发给青芽';
        assistantButton.addEventListener('click', () => openAssistantWithJob(job));
        action.append(assistantButton);
      }
    }
    item.append(type, main, action);
    return item;
  }));
}

async function loadJobs() {
  try {
    const data = await fetchJson('/api/jobs');
    state.jobs = new Map(data.jobs.map(job => [job.id, job]));
    renderJobs();
    const setup = data.jobs.find(job => job.kind === 'setup' && ['queued', 'running'].includes(job.status));
    if (setup) {
      elements.setupPanel.classList.remove('hidden');
      elements.setupButton.disabled = true;
      elements.setupButton.textContent = `${setup.progress}% 初始化中`;
      elements.setupMessage.textContent = setup.stage;
    }
    const terminalSetup = data.jobs.find(job => job.kind === 'setup' && ['completed', 'failed'].includes(job.status));
    if (!setup) {
      elements.setupButton.disabled = false;
    }
    if (terminalSetup && state.setupTerminalSeen !== terminalSetup.id) {
      state.setupTerminalSeen = terminalSetup.id;
      await loadSystem();
    }
  } catch (error) {
    showToast(error.message, 'error');
  }
}

async function startSetup() {
  elements.setupButton.disabled = true;
  elements.setupButton.textContent = '正在创建任务';
  try {
    const job = await fetchJson('/api/setup', { method: 'POST' });
    state.jobs.set(job.id, job);
    renderJobs();
    showToast('模型初始化已开始，首次下载可能需要较长时间。');
  } catch (error) {
    elements.setupButton.disabled = false;
    elements.setupButton.textContent = '重新初始化';
    showToast(error.message, 'error');
  }
}

async function uploadFile(file) {
  if (!file) return;
  const limitMb = state.system?.config?.limits?.max_upload_mb || 1024;
  if (file.size > limitMb * 1024 * 1024) {
    showToast(`文件超过 ${limitMb} MB 限制。`, 'error');
    return;
  }
  elements.dropZone.disabled = true;
  const original = elements.dropZone.querySelector('strong').textContent;
  elements.dropZone.querySelector('strong').textContent = `正在上传 ${file.name}`;
  try {
    const job = await fetchJson('/api/jobs', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-Filename': encodeURIComponent(file.name),
      },
      body: file,
    });
    state.jobs.set(job.id, job);
    renderJobs();
    document.querySelector('.jobs-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    showToast('文件已进入本地处理队列。');
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    elements.dropZone.disabled = false;
    elements.dropZone.querySelector('strong').textContent = original;
    elements.fileInput.value = '';
  }
}

elements.dropZone.addEventListener('click', () => elements.fileInput.click());
elements.fileInput.addEventListener('change', event => uploadFile(event.target.files[0]));
elements.dropZone.addEventListener('dragover', event => {
  event.preventDefault();
  elements.dropZone.classList.add('dragging');
});
elements.dropZone.addEventListener('dragleave', () => elements.dropZone.classList.remove('dragging'));
elements.dropZone.addEventListener('drop', event => {
  event.preventDefault();
  elements.dropZone.classList.remove('dragging');
  uploadFile(event.dataTransfer.files[0]);
});
elements.setupButton.addEventListener('click', startSetup);
elements.refreshButton.addEventListener('click', async () => {
  await Promise.all([loadSystem(), loadJobs()]);
  showToast('运行状态已刷新。');
});

(async function initialize() {
  await Promise.all([loadSystem(), loadJobs()]);
  state.polling = window.setInterval(loadJobs, 1500);
})();





function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function renderInlineMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  html = html.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  return html;
}

const MD_LIST = /^\s*([-*+]|\d+[.、)])\s+/;
const MD_BLOCK = /^\s*(```|#{1,6}\s|([-*+]|\d+[.、)])\s)/;

function renderMarkdown(content) {
  const container = document.createElement('div');
  container.className = 'assistant-markdown';
  const lines = content.split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      const code = document.createElement('pre');
      const inner = document.createElement('code');
      const buffer = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) {
        buffer.push(lines[i]);
        i += 1;
      }
      i += 1;
      inner.textContent = buffer.join('\n');
      code.append(inner);
      container.append(code);
      continue;
    }
    const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = Math.min(heading[1].length, 4);
      const title = document.createElement(level <= 2 ? 'h4' : 'h5');
      title.innerHTML = renderInlineMarkdown(heading[2]);
      container.append(title);
      i += 1;
      continue;
    }
    if (MD_LIST.test(line)) {
      const ordered = /^\s*\d+[.、)]\s+/.test(line);
      const list = document.createElement(ordered ? 'ol' : 'ul');
      while (i < lines.length && MD_LIST.test(lines[i])) {
        const li = document.createElement('li');
        li.innerHTML = renderInlineMarkdown(lines[i].replace(MD_LIST, ''));
        list.append(li);
        i += 1;
      }
      container.append(list);
      continue;
    }
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const para = document.createElement('p');
    const buffer = [];
    while (i < lines.length && lines[i].trim() && !MD_BLOCK.test(lines[i])) {
      buffer.push(lines[i]);
      i += 1;
    }
    para.innerHTML = renderInlineMarkdown(buffer.join('\n')).replace(/\n/g, '<br>');
    container.append(para);
  }
  return container;
}

function renderAssistantMessage(role, content) {
  const item = document.createElement('div');
  item.className = `assistant-message ${role}`;
  const mark = document.createElement('span');
  mark.className = 'assistant-message-mark';
  mark.textContent = role === 'assistant' ? '芽' : '我';
  const body = document.createElement('div');
  body.className = 'assistant-message-body';
  if (role === 'assistant') {
    body.append(renderMarkdown(content));
  } else {
    body.textContent = content;
  }
  item.append(mark, body);
  if (role === 'assistant') {
    const copy = document.createElement('button');
    copy.className = 'assistant-message-copy';
    copy.type = 'button';
    copy.textContent = '复制';
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(content);
        copy.textContent = '已复制';
        window.setTimeout(() => { copy.textContent = '复制'; }, 1500);
      } catch (_) {
        showToast('浏览器未授权复制，请手动选择文本。', 'error');
      }
    });
    body.append(copy);
  }
  elements.assistantMessages.append(item);
}

function scrollAssistantMessages() {
  elements.assistantMessages.scrollTop = elements.assistantMessages.scrollHeight;
}

function resetAssistantConversation() {
  state.assistantMessages = [];
  elements.assistantMessages.replaceChildren();
  const greeting = '你好，我是青芽。\n我能读取当前运行环境、模型配置和最近失败任务，帮你定位依赖、启动、Ollama、OCR、翻译和回嵌问题。把报错直接粘贴过来就行。';
  state.assistantMessages.push({ role: 'assistant', content: greeting });
  renderAssistantMessage('assistant', greeting);
  scrollAssistantMessages();
}

async function loadAssistantContext() {
  try {
    const context = await fetchJson('/api/assistant/context');
    elements.assistantModel.textContent = `本地 Ollama · ${context.model || '未配置模型'}`;
    elements.assistantModel.className = context.translation_ready ? 'ready' : 'warn';
  } catch (error) {
    elements.assistantModel.textContent = '助手状态读取失败';
    elements.assistantModel.className = 'warn';
  }
}

function openAssistant(prefill = '') {
  if (!state.assistantMessages.length) resetAssistantConversation();
  elements.assistantPanel.classList.add('open');
  elements.assistantPanel.setAttribute('aria-hidden', 'false');
  elements.assistantToggle.setAttribute('aria-expanded', 'true');
  if (prefill) {
    elements.assistantInput.value = prefill;
  }
  loadAssistantContext();
  window.setTimeout(() => elements.assistantInput.focus(), 80);
}

function closeAssistant() {
  elements.assistantPanel.classList.remove('open');
  elements.assistantPanel.setAttribute('aria-hidden', 'true');
  elements.assistantToggle.setAttribute('aria-expanded', 'false');
}

function openAssistantWithJob(job) {
  const details = [
    `任务文件：${job.filename || '未知'}`,
    `任务状态：${job.status || '未知'}，阶段：${job.stage || '未知'}`,
    job.message ? `错误信息：${job.message}` : '',
    job.details?.log ? `日志路径：${job.details.log}` : '',
    '请结合青穗翻译台当前项目上下文，判断根因并给出修复步骤。',
  ].filter(Boolean).join('\n');
  openAssistant(details);
  window.setTimeout(sendAssistantMessage, 120);
}

function setAssistantTyping(show) {
  const existing = elements.assistantMessages.querySelector('.assistant-message.typing');
  if (!show) {
    existing?.remove();
    return;
  }
  if (existing) return;
  const item = document.createElement('div');
  item.className = 'assistant-message assistant typing';
  const mark = document.createElement('span');
  mark.className = 'assistant-message-mark';
  mark.textContent = '芽';
  const body = document.createElement('div');
  body.className = 'assistant-message-body';
  body.append(document.createElement('i'), document.createElement('i'), document.createElement('i'));
  item.append(mark, body);
  elements.assistantMessages.append(item);
  scrollAssistantMessages();
}

async function sendAssistantMessage() {
  if (state.assistantBusy) return;
  const message = elements.assistantInput.value.trim();
  if (!message) {
    showToast('请先粘贴报错或输入问题。', 'error');
    elements.assistantInput.focus();
    return;
  }
  const conversation = state.assistantMessages.map(item => ({ ...item }));
  state.assistantBusy = true;
  elements.assistantInput.value = '';
  elements.assistantSend.disabled = true;
  elements.assistantPaste.disabled = true;
  state.assistantMessages.push({ role: 'user', content: message });
  renderAssistantMessage('user', message);
  setAssistantTyping(true);
  scrollAssistantMessages();
  try {
    const response = await fetchJson('/api/assistant/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, conversation }),
    });
    setAssistantTyping(false);
    const reply = response.reply || '助手没有返回可读答复。';
    state.assistantMessages.push({ role: 'assistant', content: reply });
    renderAssistantMessage('assistant', reply);
  } catch (error) {
    setAssistantTyping(false);
    const reply = `暂时没能调用本地模型。\n${error.message}`;
    state.assistantMessages.push({ role: 'assistant', content: reply });
    renderAssistantMessage('assistant', reply);
  } finally {
    state.assistantBusy = false;
    elements.assistantSend.disabled = false;
    elements.assistantPaste.disabled = false;
    scrollAssistantMessages();
  }
}

async function pasteAndAnalyze() {
  try {
    const text = await navigator.clipboard.readText();
    if (!text.trim()) {
      showToast('剪贴板没有文本内容。', 'error');
      return;
    }
    elements.assistantInput.value = text.slice(0, 12000);
    await sendAssistantMessage();
  } catch (_) {
    showToast('浏览器未授权读取剪贴板，请先点击输入框后按 Ctrl + V。', 'error');
    elements.assistantInput.focus();
  }
}

elements.assistantToggle.addEventListener('click', () => {
  if (elements.assistantPanel.classList.contains('open')) closeAssistant();
  else openAssistant();
});
elements.assistantClose.addEventListener('click', closeAssistant);
elements.assistantClear.addEventListener('click', resetAssistantConversation);
elements.assistantSend.addEventListener('click', sendAssistantMessage);
elements.assistantPaste.addEventListener('click', pasteAndAnalyze);
elements.assistantInput.addEventListener('keydown', event => {
  if (event.key === 'Enter' && event.ctrlKey) {
    event.preventDefault();
    sendAssistantMessage();
  }
});
document.querySelectorAll('[data-assistant-prompt]').forEach(button => {
  button.addEventListener('click', () => {
    openAssistant(button.dataset.assistantPrompt || '');
    window.setTimeout(sendAssistantMessage, 100);
  });
});
resetAssistantConversation();

