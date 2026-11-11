(() => {
  const form = document.querySelector('[data-it-booking]');
  if (!form) return;

  const states = {
    NSW: 'New South Wales', VIC: 'Victoria', QLD: 'Queensland', SA: 'South Australia',
    WA: 'Western Australia', TAS: 'Tasmania', ACT: 'Australian Capital Territory', NT: 'Northern Territory'
  };
  const cache = new Map();
  let retryAfter = 0;

  function streetQuery(value) {
    // Keep local details such as "Unit 3, " or "2/" when selecting a mapped street.
    const unit = value.match(/^(?:(?:unit|shop|suite|factory|warehouse|level|lot)\s+[\w-]+\s*[,/ ]\s*|[\da-z]+\/\s*)(?=\d)/i);
    const prefix = unit ? unit[0] : '';
    const query = value.slice(prefix.length).trim();
    const number = query.match(/^(\d+[a-z]?(?:\s*[-–]\s*\d+[a-z]?)?)\s+/i);
    return {query, prefix, number: number ? number[1] : ''};
  }

  function matches(features, typed, requiredState, preferredState) {
    const unique = new Map();
    for (const {properties: p} of features) {
      if (!p || p.countrycode?.toUpperCase() !== 'AU') continue;
      const state = Object.keys(states).find(code => code === p.state?.toUpperCase() || states[code].toLowerCase() === p.state?.toLowerCase());
      if (!state || (requiredState && state !== requiredState)) continue;
      const street = p.street || (p.type === 'street' && p.name);
      if (!street) continue;
      if (typed.number && p.housenumber && typed.number.toLowerCase() !== p.housenumber.toLowerCase()) continue;

      const number = p.housenumber || typed.number;
      const line = typed.prefix + [number, street].filter(Boolean).join(' ');
      const suburb = p.district || p.locality || p.city;
      const region = [state, p.postcode].filter(Boolean).join(' ');
      const address = [line, suburb, region].filter(Boolean).join(', ');
      const streetOnly = Boolean(typed.number && !p.housenumber);
      if (address.length <= 500 && (!unique.has(address) || !streetOnly)) {
        unique.set(address, {address, state, streetOnly});
      }
    }
    return [...unique.values()].sort((a,b) => Number(b.state === preferredState) - Number(a.state === preferredState)).slice(0, 6);
  }

  for (const field of form.querySelectorAll('[data-address-state]')) {
    const input = field.querySelector('input');
    const list = field.querySelector('[role=listbox]');
    const status = field.querySelector('[role=status]');
    const stateSelect = form.elements[field.dataset.addressState];
    let timer, controller, revision = 0, active = -1, rows = [], selectedState = '';

    function close() {
      clearTimeout(timer);
      timer = null;
      controller?.abort();
      controller = null;
      revision++;
      active = -1;
      rows = [];
      list.replaceChildren();
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      status.textContent = '';
    }

    function localDestination() {
      return stateSelect === form.elements.destination && form.elements.vehicle.value === 'RIGID' && !form.elements.hours.disabled;
    }

    function select(index) {
      const item = rows[index];
      if (!item) return;
      close();
      input.value = item.address;
      selectedState = item.state;
      input.setCustomValidity('');
      if (localDestination() && !form.elements.origin.value) {
        form.elements.origin.value = item.state;
        form.elements.origin.dispatchEvent(new Event('change', {bubbles:true}));
      }
      stateSelect.value = item.state;
      stateSelect.dispatchEvent(new Event('change', {bubbles:true}));
      status.textContent = item.streetOnly ? 'Street matched. Check the street number.' : 'Address selected.';
    }

    async function lookup(query, requiredState, signal) {
      const url = new URL('https://photon.komoot.io/api/');
      if (requiredState && !query.toLowerCase().includes(states[requiredState].toLowerCase())) {
        query += ' ' + states[requiredState];
      }
      url.search = new URLSearchParams({q:query, countrycode:'au', lang:'en', limit:'15'});
      url.searchParams.append('layer','house');
      url.searchParams.append('layer','street');
      const key = url.href;
      if (cache.has(key)) return cache.get(key);
      if (Date.now() < retryAfter) throw new Error();
      const response = await fetch(url, {signal, credentials:'omit', referrerPolicy:'no-referrer'});
      if (response.status === 429) retryAfter = Date.now() + 60000;
      if (!response.ok) throw new Error();
      const features = (await response.json()).features;
      if (!Array.isArray(features)) throw new Error();
      if (cache.size >= 40) cache.delete(cache.keys().next().value);
      cache.set(key, features);
      return features;
    }

    async function search(version) {
      const typed = streetQuery(input.value.trim());
      const requiredState = localDestination() ? form.elements.origin.value : '';
      const request = new AbortController();
      controller = request;
      const timeout = setTimeout(() => request.abort(), 10000);
      try {
        let features = await lookup(typed.query, requiredState, request.signal);
        if (version !== revision) return;
        let suggestions = matches(features, typed, requiredState, stateSelect.value);
        if (!suggestions.length && typed.number) {
          const street = typed.query.slice(typed.number.length).trimStart();
          features = await lookup(street, requiredState, request.signal);
          if (version !== revision) return;
          suggestions = matches(features, typed, requiredState, stateSelect.value);
        }
        rows = suggestions;
        rows.forEach((row, index) => {
          const option = document.createElement('li');
          option.id = list.id + '-' + index;
          option.setAttribute('role','option');
          option.setAttribute('aria-selected','false');
          option.textContent = row.address;
          option.addEventListener('click', () => select(index));
          list.append(option);
        });
        list.hidden = rows.length === 0;
        input.setAttribute('aria-expanded', String(rows.length > 0));
        status.textContent = rows.length ? rows.length + (rows.length === 1 ? ' suggestion.' : ' suggestions.')
          : 'No match. Add the suburb or enter the address manually.';
      } catch {
        if (version === revision) status.textContent = 'Suggestions unavailable. You can enter the address manually.';
      } finally {
        clearTimeout(timeout);
        if (controller === request) controller = null;
      }
    }

    input.addEventListener('input', () => {
      close();
      selectedState = '';
      input.setCustomValidity('');
      const query = streetQuery(input.value.trim()).query;
      if (query.length < 3 || !/[a-z]{2}/i.test(query)) return;
      const version = revision;
      timer = setTimeout(() => search(version), 500);
    });
    input.addEventListener('blur', close);
    // Keep the input focused until a mouse selection has been applied.
    list.addEventListener('mousedown', e => e.preventDefault());
    input.addEventListener('keydown', e => {
      if (e.key === 'Escape') { close(); return; }
      if (list.hidden) return;
      if (e.key === 'Enter' && active >= 0) { e.preventDefault(); select(active); return; }
      if (!['ArrowDown','ArrowUp'].includes(e.key)) return;
      e.preventDefault();
      if (active >= 0) list.children[active].setAttribute('aria-selected','false');
      active = e.key === 'ArrowDown' ? (active+1) % rows.length : (active <= 0 ? rows.length-1 : active-1);
      const option = list.children[active];
      option.setAttribute('aria-selected','true');
      option.scrollIntoView({block:'nearest'});
      input.setAttribute('aria-activedescendant', option.id);
    });

    form.addEventListener('change', event => {
      if (![form.elements.origin,form.elements.destination,form.elements.vehicle].includes(event.target)) return;
      if (rows.length || controller || timer) close();
      input.setCustomValidity(selectedState && selectedState !== stateSelect.value
        ? 'Update the address or choose its matching state.' : '');
    });
  }
})();
