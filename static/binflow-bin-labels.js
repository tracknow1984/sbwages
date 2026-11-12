'use strict';
(() => {
  const picker = document.querySelector('[data-label-picker]');
  if (picker) {
    const options = [...picker.querySelectorAll('.label-option')].map(label => ({
      label, box: label.querySelector('input'), searchText: label.textContent.toLowerCase()
    }));
    const search = picker.querySelector('[data-bin-search]');
    function refresh() {
      const count = options.filter(item => item.box.checked).length;
      const query = search ? search.value.trim().toLowerCase() : '';
      let matches = 0;
      for (const item of options) {
        const match = item.searchText.includes(query);
        if (match) matches++;
        item.label.hidden = !match && !item.box.checked;
        item.box.disabled = !item.box.checked && count >= 4;
      }
      if (search) picker.querySelector('[data-no-match]').hidden = matches > 0;
      picker.querySelector('[data-batch-count]').textContent = count + ' of 4 selected';
      picker.querySelector('[type="submit"]').disabled = count !== 4;
    }
    if (search) {
      search.closest('label').hidden = false;
      search.addEventListener('input', refresh);
    }
    picker.addEventListener('change', refresh);
    window.addEventListener('pageshow', refresh);
    refresh();
  }

  const printButton = document.querySelector('[data-sheet-print]');
  if (printButton) {
    printButton.addEventListener('click', () => window.print());
    window.addEventListener('load', async () => {
      try {
        await Promise.all([...document.querySelectorAll('.bin-sticker img')].map(img => img.decode()));
      } catch {
        document.querySelector('[data-sheet-error]').hidden = false;
        return;
      }
      printButton.disabled = false;
      window.print();
    });
  }
})();
