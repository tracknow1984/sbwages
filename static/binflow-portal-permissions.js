document.querySelectorAll('.it-permissions').forEach(table => {
  const form = table.closest('form');
  form.addEventListener('change', event => {
    const input = event.target;
    if (!['permission','pod_scope'].includes(input.name)) return;
    const custom = form.elements.custom_permissions;
    if (custom) custom.checked = true;
    if (input.name !== 'permission') return;
    const [area, action] = input.value.split('.');
    const boxes = Array.from(table.querySelectorAll('input'));
    if (input.checked && action !== 'view') boxes.find(box => box.value === area + '.view').checked = true;
    if (!input.checked && action === 'view') {
      boxes.filter(box => box.value.startsWith(area + '.')).forEach(box => { box.checked = false; });
    }
  });
});
