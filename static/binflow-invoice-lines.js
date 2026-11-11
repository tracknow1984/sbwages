const addLine = document.getElementById('add-invoice-line');
const lines = document.getElementById('invoice-lines');
const template = document.getElementById('invoice-line');
addLine.hidden = false;
addLine.addEventListener('click', () => {
  lines.append(template.content.cloneNode(true));
  lines.lastElementChild.querySelector('input').focus();
  addLine.disabled = lines.children.length >= 50;
});
