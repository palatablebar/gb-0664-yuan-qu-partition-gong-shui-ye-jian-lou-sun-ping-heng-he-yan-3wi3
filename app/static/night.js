const feedback = document.querySelector('#night-feedback');
const readingResult = document.querySelector('#reading-result');

async function send(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail?.message || data.message || '请求失败，请重试');
  return data;
}

async function get(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail?.message || data.message || '请求失败，请重试');
  return data;
}

function busy(button, text) {
  if (!button) return () => {};
  const label = button.textContent;
  button.disabled = true;
  button.textContent = text;
  return () => {
    button.disabled = false;
    button.textContent = label;
  };
}

document.querySelector('#backfill-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const done = busy(event.target.querySelector('button'), '登记中…');
  try {
    const data = await send(`/api/night/${event.target.dataset.night}/backfill`, { text: form.get('text') });
    feedback.textContent = data.duplicate ? '相同补录已登记过，未重复计入。' : `补录 ${data.key.slice(0, 12)} 已登记，等待选择重开或移交。`;
    if (!data.duplicate) location.reload();
  } catch (error) {
    feedback.textContent = error.message;
  } finally {
    done();
  }
});

document.querySelector('#publish-button')?.addEventListener('click', async (event) => {
  const done = busy(event.target, '发布中…');
  try {
    const data = await send(`/api/night/${event.target.dataset.night}/publish`);
    feedback.textContent = data.duplicate ? '相同依据已发布过，未新增修订。' : '已发布正式结论。';
    location.reload();
  } catch (error) {
    feedback.textContent = error.message;
    done();
  }
});

document.querySelectorAll('.decision-form').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    const done = busy(form.querySelector('button'), '处理中…');
    const payload = {};
    if (form.dataset.payload) {
      const input = form.querySelector(`[name="${form.dataset.payload}"]`);
      if (input?.value.trim()) payload[form.dataset.payload] = input.value.trim();
    }
    try {
      const data = await send(form.dataset.url, payload);
      feedback.textContent = data.duplicate ? '该补录已按同一方式处理过，未重复计入。' : '已记录处理结果。';
      location.reload();
    } catch (error) {
      feedback.textContent = error.message;
      done();
    }
  });
});

document.querySelector('#reading-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const recordId = new FormData(event.target).get('record_id').trim();
  const done = busy(event.target.querySelector('button'), '查找中…');
  try {
    const data = await get(`/api/night/${event.target.dataset.night}/readings/${encodeURIComponent(recordId)}`);
    const rows = data.versions.map((item) => `<tr><td>${item.source}</td><td>${item.revision ?? '—'}</td><td class="num">${item.value_m3}</td><td>${item.origin}</td><td class="mono small">${(item.backfill_key || '').slice(0, 12) || '—'}</td><td>${item.created_at}</td></tr>`).join('');
    readingResult.innerHTML = `<h3>${data.record_id} 历次取值</h3><div class="table-scroll"><table class="ledger"><thead><tr><th>来源</th><th>版本</th><th>原值 / m³</th><th>当前状态</th><th>补录键</th><th>时间</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    feedback.textContent = '';
  } catch (error) {
    readingResult.innerHTML = '';
    feedback.textContent = error.message;
  } finally {
    done();
  }
});
