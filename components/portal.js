async function initPortal({ sidebar, role, defaultPage }) {
  try {
    // 1. Who is logged in?
    const res = await fetch('/api/current-user');
    if (!res.ok) throw new Error('Not logged in');
    const user = await res.json();
    if (user.role !== role) throw new Error('Wrong role');

    // 2. Load the sidebar
    const html = await (await fetch(sidebar)).text();
    document.getElementById('sidebar-container').innerHTML = html;

    // 3. Fill every [data-user] element on the page (sidebar + page header)
    const values = {
      name: user.name,
      id: user.id_number,
      title: user.title,
      sub: role === 'student' ? 'Reg No: ' + user.id_number : user.title
    };
    document.querySelectorAll('[data-user]').forEach(el => {
      el.textContent = values[el.dataset.user] ?? '';
    });

    // 4. Highlight current page
    const currentPage = window.location.pathname.split('/').pop() || defaultPage;
    document.querySelectorAll('.sidebar-menu a').forEach(link => {
      link.classList.toggle('active', link.dataset.page === currentPage);
    });
  } catch (err) {
    console.error(err);
    window.location.href = '/auth/login.html';
  }
}