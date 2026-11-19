(() => {
  const form = document.querySelector('[data-invoice-profile]');
  if (!form) return;
  const client = form.elements.client;
  const fields = form.querySelector('[data-contact-fields]');
  function updateContact() {
    if (!fields) return;
    const needed = !client || (client.value && client.selectedOptions[0].dataset.linked === '0');
    fields.hidden = !needed;
    form.elements.new_contact.disabled = !needed;
    form.elements.new_contact.required = Boolean(needed);
  }
  updateContact();
  client?.addEventListener('change', () => {
    const customer = client.selectedOptions[0];
    const choose = (field, prior, fallback) => {
      field.value = prior || fallback;
      if (!field.value) field.value = fallback;
    };
    choose(form.elements.account_code, customer.dataset.account, form.dataset.defaultAccount);
    choose(form.elements.tax_type, customer.dataset.tax, form.dataset.defaultTax);
    if (form.elements.new_contact) form.elements.new_contact.value = '';
    updateContact();
  });
})();
