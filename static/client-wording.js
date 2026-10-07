(() => {
  // Presentation-only: retain original customer records and historical submissions.
  const clean = value => value.replace(/\bBlockTexx\s*[-–—]\s*/gi, '').replace(/\bBlockTexx\b/gi, 'Client');
  const attributes = ['title', 'aria-label', 'placeholder', 'alt'];
  function visit(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      if (!node.parentElement?.closest('script,style,textarea,[contenteditable]')) {
        const text = clean(node.data);
        if (text !== node.data) node.data = text;
      }
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      for (const key of attributes) {
        const value = node.getAttribute(key);
        if (value && clean(value) !== value) node.setAttribute(key, clean(value));
      }
      for (const child of node.childNodes) visit(child);
    }
  }
  visit(document.documentElement);
  new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'characterData' || record.type === 'attributes') visit(record.target);
      else for (const node of record.addedNodes) visit(node);
    }
  }).observe(document.documentElement, {subtree:true, childList:true, characterData:true, attributes:true, attributeFilter:attributes});
})();
