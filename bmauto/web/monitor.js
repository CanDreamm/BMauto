(() => {
  let loading = false;
  async function refresh() {
    if (loading || document.hidden) return;
    loading = true;
    try {
      const url = new URL(window.location.href);
      const project = document.querySelector('.project-tab.active');
      const month = document.querySelector('.month-tab.active');
      if (project) url.searchParams.set('project', new URL(project.href).searchParams.get('project'));
      else url.searchParams.delete('project');
      if (month) url.searchParams.set('month', new URL(month.href).searchParams.get('month'));
      else url.searchParams.delete('month');
      url.searchParams.delete('refresh');
      const response = await fetch(url.href, {cache: 'no-store', credentials: 'omit'});
      if (response.status === 404) {
        window.location.replace(url.origin + url.pathname);
        return;
      }
      if (!response.ok) throw new Error('unavailable');
      const next = new DOMParser().parseFromString(await response.text(), 'text/html');
      const ids = ['project-tabs', 'project-title', 'plan-rows', 'update-status', 'month-title', 'month-tabs'];
      const elements = ids.map(id => next.getElementById(id));
      if (elements.some(element => !element)) throw new Error('invalid response');
      const projectScroll = document.getElementById('project-tabs').scrollLeft;
      const monthScroll = document.getElementById('month-tabs').scrollLeft;
      ids.forEach((id, index) => document.getElementById(id).replaceWith(elements[index]));
      document.getElementById('project-tabs').scrollLeft = projectScroll;
      document.getElementById('month-tabs').scrollLeft = monthScroll;
      document.title = document.getElementById('project-title').textContent + ' · Мониторинг';
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
  window.addEventListener('pageshow', event => { if (event.persisted) refresh(); });
  document.querySelectorAll('.project-tab.active, .month-tab.active').forEach(active => {
    active.scrollIntoView({block: 'nearest', inline: 'nearest'});
  });
})();
