(() => {
  let loading = false;
  async function refresh() {
    if (loading || document.hidden) return;
    loading = true;
    try {
      const url = new URL(window.location.href);
      const activeTab = document.querySelector('.month-tab.active');
      if (activeTab) url.searchParams.set('month', new URL(activeTab.href).searchParams.get('month'));
      url.searchParams.delete('refresh');
      const response = await fetch(url.href, {cache: 'no-store', credentials: 'omit'});
      if (!response.ok) throw new Error('unavailable');
      const next = new DOMParser().parseFromString(await response.text(), 'text/html');
      const ids = ['stats', 'plan-rows', 'update-status', 'month-title'];
      const elements = ids.map(id => next.getElementById(id));
      if (elements.some(element => !element)) throw new Error('invalid response');
      ids.forEach((id, index) => document.getElementById(id).replaceWith(elements[index]));
    } catch (_) {
      const status = document.getElementById('update-status');
      status.classList.add('failed');
      status.textContent = 'Не удалось обновить · нажмите «Обновить»';
    } finally {
      loading = false;
    }
  }
  setInterval(refresh, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  const active = document.querySelector('.month-tab.active');
  if (active) active.scrollIntoView({block: 'nearest', inline: 'nearest'});
})();
