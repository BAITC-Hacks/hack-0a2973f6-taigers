const messages = document.querySelector('#messages');
const form = document.querySelector('#chatForm');
const input = document.querySelector('#messageInput');
const fileInput = document.querySelector('#fileInput');
const fileChip = document.querySelector('#fileChip');
const sendButton = document.querySelector('#sendButton');
const modeLabel = document.querySelector('#modeLabel');
const specDialog = document.querySelector('#specDialog');
const specContent = document.querySelector('#specContent');
const specPrepare = document.querySelector('#specPrepare');
const specSelection = document.querySelector('#specSelection');
const sessionId = localStorage.getItem('ekt_session') || crypto.randomUUID();
localStorage.setItem('ekt_session', sessionId);
document.querySelector('#cartLink').href = `/cart.html?session=${encodeURIComponent(sessionId)}`;
let history = [];
let currentAnalysis = null;

fileInput.addEventListener('change', async () => {
  const file = fileInput.files[0];
  if (!file) return;
  specDialog.showModal();
  specContent.innerHTML = '<div class="spec-loading"><div><b>Разбираю спецификацию</b><p>Извлекаю позиции, сверяю каталог и строю план по складам…</p></div></div>';
  specPrepare.disabled = true;
  specSelection.textContent = 'Анализ выполняется';
  const body = new FormData();
  body.append('file', file);
  try {
    const response = await fetch('/api/spec/analyze', {method: 'POST', body});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось разобрать спецификацию');
    currentAnalysis = data;
    renderSpecification(data);
  } catch (error) {
    specContent.innerHTML = `<div class="spec-error">${escapeHtml(error.message)}</div>`;
    specSelection.textContent = 'Ошибка анализа';
  } finally {
    fileInput.value = '';
  }
});

document.querySelector('#specClose').addEventListener('click', () => specDialog.close());
specDialog.addEventListener('click', event => {
  if (event.target === specDialog) specDialog.close();
});

specPrepare.addEventListener('click', async () => {
  const lineIds = [...specContent.querySelectorAll('.spec-check:checked')].map(item => item.value);
  if (!lineIds.length || !currentAnalysis) return;
  specPrepare.disabled = true;
  specPrepare.textContent = 'Проверяю остатки…';
  try {
    const response = await fetch('/api/spec/prepare-cart', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sessionId, analysis_id: currentAnalysis.id, line_ids: lineIds}),
    });
    const pending = await response.json();
    if (!response.ok) throw new Error(pending.detail || 'Не удалось подготовить корзину');
    specDialog.close();
    const node = addMessage('assistant', `Спецификация проверена. Подготовлено ${pending.items.length} позиций на сумму ${money(pending.total)}. Проверьте итог и подтвердите добавление.`);
    renderConfirmation(node, pending);
  } catch (error) {
    specContent.insertAdjacentHTML('afterbegin', `<div class="spec-error">${escapeHtml(error.message)}</div>`);
  } finally {
    specPrepare.textContent = 'Подготовить корзину';
    updateSpecSelection();
  }
});

form.addEventListener('submit', async event => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  addMessage('user', message);
  input.value = '';
  sendButton.disabled = true;
  const typing = addMessage('assistant', 'Проверяю каталог…', 'typing');
  try {
    const response = await fetch('/api/chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sessionId, message, history, image_data_url: null}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Ошибка ассистента');
    typing.remove();
    const node = addMessage('assistant', data.answer);
    renderProducts(node, data.products || []);
    if (data.pending_cart) renderConfirmation(node, data.pending_cart);
    history.push({role: 'user', content: message}, {role: 'assistant', content: data.answer});
    history = history.slice(-10);
    modeLabel.textContent = data.mode === 'fallback'
      ? 'Демо-режим: добавьте OPENAI_API_KEY для свободного диалога'
      : 'Ответ создан ИИ на основе проверенных данных каталога';
  } catch (error) {
    typing.remove();
    addMessage('assistant', `Не удалось выполнить запрос: ${error.message}`);
  }
  sendButton.disabled = false;
  input.focus();
  refreshCart();
});

function renderSpecification(analysis) {
  const summary = analysis.summary;
  specContent.innerHTML = `
    <div class="spec-kpis">
      ${kpi(summary.total_lines, 'Позиций')}
      ${kpi(summary.auto_matched, 'Готовы к заказу')}
      ${kpi(summary.needs_review, 'Требуют проверки')}
      ${kpi(summary.stock_issues, 'Проблемы остатка')}
      ${kpi(summary.coverage_percent + '%', 'Автопокрытие')}
    </div>
    <p><strong>${escapeHtml(analysis.document_title || analysis.filename || 'Спецификация')}</strong> · Риск: <b class="risk-${summary.risk_level}">${riskLabel(summary.risk_level)}</b> · Предварительная сумма: ${money(summary.order_total)}</p>
    <div id="specLines"></div>
    <p class="spec-audit">Источник: API ekt.kz · Проверено ${new Date(analysis.audit.catalog_checked_at).toLocaleString('ru-RU')} · ${escapeHtml(analysis.audit.policy)}</p>`;
  const root = specContent.querySelector('#specLines');
  analysis.lines.forEach(line => root.append(renderSpecLine(line)));
  specContent.querySelectorAll('.spec-check').forEach(box => box.addEventListener('change', updateSpecSelection));
  updateSpecSelection();
}

function renderSpecLine(line) {
  const article = line.article ? `Артикул из файла: ${escapeHtml(line.article)}` : 'Артикул в файле не указан';
  const product = line.product;
  const stock = product ? `${line.quantity} шт. из ${Math.floor(product.quantity)} доступных` : 'Совпадение не найдено';
  const warehouse = line.fulfillment?.single_warehouse_options?.[0]
    ? `Можно собрать на одном складе: ${escapeHtml(line.fulfillment.single_warehouse_options[0].name)}`
    : line.fulfillment?.fully_coverable ? `Нужна комплектация с ${line.fulfillment.split_plan.length} складов` : 'Недостаточный остаток';
  const alternative = line.alternatives?.[0]
    ? `<p>Альтернатива: <b>${escapeHtml(line.alternatives[0].name)}</b></p>` : '';
  const element = document.createElement('article');
  element.className = 'spec-line';
  element.innerHTML = `
    <input class="spec-check" type="checkbox" value="${line.id}" ${line.selected_by_default ? 'checked' : ''} ${!product || line.status === 'review' || line.status === 'not_found' ? 'disabled' : ''}>
    <div><h3>${escapeHtml(line.requested_name)}</h3><p>${article}</p><p>Запрошено: ${line.quantity} ${escapeHtml(line.unit || 'шт')}</p></div>
    <div>${product ? `<h3 class="match">${escapeHtml(product.name)}</h3><p>${escapeHtml(product.article)} · ${money(product.price)}</p>` : '<h3>Нет подтверждённого совпадения</h3>'}<p>${escapeHtml(line.reason)}</p>${alternative}<p class="fulfillment">${warehouse}</p></div>
    <div><span class="status ${line.status}">${statusLabel(line.status)}</span><p class="confidence">${Math.round(line.confidence * 100)}%</p></div>
    <div><strong>${stock}</strong><p class="risk-${line.risk}">Риск: ${riskLabel(line.risk)}</p></div>`;
  return element;
}

function updateSpecSelection() {
  const checked = [...specContent.querySelectorAll('.spec-check:checked')];
  const total = currentAnalysis ? currentAnalysis.lines
    .filter(line => checked.some(box => box.value === line.id))
    .reduce((sum, line) => sum + (line.product?.price || 0) * line.quantity, 0) : 0;
  specSelection.textContent = `${checked.length} позиций · ${money(total)}`;
  specPrepare.disabled = checked.length === 0;
}

function addMessage(role, text, extra = '') {
  const article = document.createElement('article');
  article.className = `message ${role} ${extra}`;
  const div = document.createElement('div');
  if (role === 'assistant') div.innerHTML = formatAssistant(text); else div.textContent = text;
  article.append(div);
  messages.append(article);
  messages.scrollTop = messages.scrollHeight;
  return article;
}

function renderProducts(node, products) {
  if (!products.length) return;
  const root = document.createElement('div');
  root.className = 'products';
  products.slice(0, 4).forEach(product => {
    const card = document.createElement('article');
    card.className = 'product-card';
    card.innerHTML = `${product.image ? `<img src="${safeUrl(product.image)}" alt="">` : ''}<div><h3>${escapeHtml(product.name)}</h3><p>Артикул: ${escapeHtml(product.article || '—')} · ${money(product.price || 0)}</p>${product.quantity !== undefined ? `<p>Остаток: ${product.quantity}</p>` : ''}${product.url ? `<a href="${safeUrl(product.url)}" target="_blank" rel="noopener">Открыть товар ↗</a>` : ''}</div>`;
    root.append(card);
  });
  node.append(root);
}

function renderConfirmation(node, pending) {
  const card = document.createElement('div');
  card.className = 'confirm-card';
  card.innerHTML = `<strong>Подтвердите добавление</strong><p>${pending.items.map(item => `${escapeHtml(item.name)} — ${item.quantity} шт.`).join('<br>')}</p><p>Итого: ${money(pending.total)}</p><button>Подтвердить добавление</button>`;
  const button = card.querySelector('button');
  button.onclick = async () => {
    button.disabled = true;
    const url = `/api/cart/confirm?session_id=${encodeURIComponent(sessionId)}&pending_id=${encodeURIComponent(pending.id)}&confirmation=${encodeURIComponent('подтвердить добавление')}`;
    const response = await fetch(url, {method: 'POST'});
    const data = await response.json();
    if (response.ok) {
      button.textContent = 'Добавлено ✓';
      addMessage('assistant', `Готово. Товары добавлены в корзину. Итого: ${money(data.total)}`);
      refreshCart();
    } else {
      button.disabled = false;
      button.textContent = 'Повторить';
      addMessage('assistant', data.detail || 'Не удалось добавить товар');
    }
  };
  node.append(card);
}

async function refreshCart() {
  try {
    const response = await fetch(`/api/cart?session_id=${encodeURIComponent(sessionId)}`);
    const cart = await response.json();
    document.querySelector('#cartCount').textContent = cart.items.reduce((sum, item) => sum + item.quantity, 0);
  } catch {}
}

function kpi(value, label) { return `<div class="spec-kpi"><b>${value}</b><small>${label}</small></div>`; }
function statusLabel(status) { return ({matched: 'Найдено', review: 'Проверить', not_found: 'Не найдено', out_of_stock: 'Нет в наличии', insufficient_stock: 'Не хватает'})[status] || status; }
function riskLabel(risk) { return ({low: 'низкий', medium: 'средний', high: 'высокий'})[risk] || risk; }
function money(value) { return new Intl.NumberFormat('ru-RU').format(value) + ' ₸'; }
function escapeHtml(value) { const div = document.createElement('div'); div.textContent = String(value ?? ''); return div.innerHTML; }
function safeUrl(value) { try { const url = new URL(value, location.origin); return ['http:', 'https:'].includes(url.protocol) ? url.href : '#'; } catch { return '#'; } }
function formatAssistant(value) {
  let text = escapeHtml(value);
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return text.replace(/\n/g, '<br>');
}

refreshCart();
