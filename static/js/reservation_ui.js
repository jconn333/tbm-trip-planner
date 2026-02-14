(() => {
  function splitIsoToDateTime(isoString){
    const dateObj = new Date(isoString);
    if (Number.isNaN(dateObj.getTime())) return { date: '', time: '' };
    const year = dateObj.getFullYear();
    const month = String(dateObj.getMonth() + 1).padStart(2, '0');
    const day = String(dateObj.getDate()).padStart(2, '0');
    const hour = String(dateObj.getHours()).padStart(2, '0');
    const minute = String(dateObj.getMinutes()).padStart(2, '0');
    return { date: `${year}-${month}-${day}`, time: `${hour}:${minute}` };
  }

  function formatLocalIsoDisplay(value, options = {}){
    const includeTime = options.includeTime !== false;
    const text = String(value || '').trim();
    if(!text) return '';
    const dateObj = new Date(text);
    if(Number.isNaN(dateObj.getTime())) return text;
    const month = String(dateObj.getMonth() + 1).padStart(2, '0');
    const day = String(dateObj.getDate()).padStart(2, '0');
    const year = dateObj.getFullYear();
    if(!includeTime){
      return `${month}-${day}-${year}`;
    }
    const hour = String(dateObj.getHours()).padStart(2, '0');
    const minute = String(dateObj.getMinutes()).padStart(2, '0');
    return `${month}-${day}-${year} ${hour}:${minute}`;
  }

  function toQuarterHour(timeValue){
    const parts = String(timeValue || '').split(':');
    if(parts.length < 2) return '';
    const hours = Number(parts[0]);
    const minutes = Number(parts[1]);
    if(Number.isNaN(hours) || Number.isNaN(minutes)) return '';
    const total = (hours * 60) + minutes;
    const rounded = Math.round(total / 15) * 15;
    const clamped = Math.max(0, Math.min(23 * 60 + 45, rounded));
    const h = Math.floor(clamped / 60);
    const m = clamped % 60;
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`;
  }

  function initQuarterHourSelect(selectEl, placeholder = 'Select a Time', defaultValue = ''){
    if(!(selectEl instanceof HTMLSelectElement)) return;
    const previousValue = selectEl.value;
    selectEl.innerHTML = `<option value="">${placeholder}</option>`;
    for(let minutes = 0; minutes < 24 * 60; minutes += 15){
      const hour = Math.floor(minutes / 60);
      const minute = minutes % 60;
      const value = `${String(hour).padStart(2,'0')}:${String(minute).padStart(2,'0')}`;
      const hour12 = hour % 12 === 0 ? 12 : hour % 12;
      const meridiem = hour < 12 ? 'AM' : 'PM';
      const label = `${hour12}:${String(minute).padStart(2,'0')} ${meridiem}`;
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      selectEl.appendChild(option);
    }
    const hasPrevious = previousValue && Array.from(selectEl.options).some((option) => option.value === previousValue);
    const hasDefault = defaultValue && Array.from(selectEl.options).some((option) => option.value === defaultValue);
    if(hasPrevious){
      selectEl.value = previousValue;
      return;
    }
    if(hasDefault){
      selectEl.value = defaultValue;
    }
  }

  function clearAirportSuggestions(target){
    target.innerHTML = '';
    target.classList.remove('open');
  }

  function setupAirportAutocomplete(input, suggestionsEl){
    let debounceTimer = null;
    async function runSearch(){
      const query = input.value.trim();
      if(query.length < 2){ clearAirportSuggestions(suggestionsEl); return; }
      const resp = await fetch(`/api/airports?q=${encodeURIComponent(query)}`);
      if(!resp.ok){ clearAirportSuggestions(suggestionsEl); return; }
      const choices = await resp.json();
      if(!Array.isArray(choices) || choices.length === 0){ clearAirportSuggestions(suggestionsEl); return; }
      suggestionsEl.innerHTML = '';
      choices.slice(0, 8).forEach((choice)=>{
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'airportOption';
        btn.textContent = `${choice.icao} - ${choice.label}`;
        btn.addEventListener('mousedown', (event)=>{ event.preventDefault(); input.value = choice.icao; clearAirportSuggestions(suggestionsEl); });
        suggestionsEl.appendChild(btn);
      });
      suggestionsEl.classList.add('open');
    }
    input.addEventListener('input', ()=>{ if(debounceTimer) clearTimeout(debounceTimer); debounceTimer = window.setTimeout(runSearch, 200); });
    input.addEventListener('blur', ()=>{ input.value = input.value.trim().toUpperCase(); setTimeout(()=>clearAirportSuggestions(suggestionsEl), 120); });
    input.addEventListener('focus', ()=>{ if(input.value.trim().length >= 2) runSearch(); });
  }

  async function apiAction(method, url, body){
    const resp = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined
    });
    const data = await resp.json().catch(() => ({}));
    if(!resp.ok){
      if(window.TBMUI){ window.TBMUI.toast(data.error || 'Action failed', { error: true }); }
      return { ok: false, data, response: resp };
    }
    return { ok: true, data, response: resp };
  }

  function enhanceSelect(selectEl){
    if(!(selectEl instanceof HTMLSelectElement)) return null;
    if(selectEl.dataset.customSelectEnhanced === '1') return null;
    selectEl.dataset.customSelectEnhanced = '1';
    selectEl.classList.add('tbmNativeSelect');

    const container = document.createElement('div');
    container.className = 'tbmCustomSelect';
    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'tbmCustomSelectBtn';
    const menu = document.createElement('div');
    menu.className = 'tbmCustomSelectMenu';

    function closeMenu(){
      menu.classList.remove('open');
      container.classList.remove('is-open');
    }

    function openMenu(){
      document.querySelectorAll('.tbmCustomSelectMenu.open').forEach((openMenuEl) => {
        if(!(openMenuEl instanceof HTMLElement)) return;
        if(openMenuEl === menu) return;
        openMenuEl.classList.remove('open');
        const parent = openMenuEl.closest('.tbmCustomSelect');
        if(parent) parent.classList.remove('is-open');
      });
      menu.classList.add('open');
      container.classList.add('is-open');
    }

    function syncTrigger(){
      const selectedOption = selectEl.options[selectEl.selectedIndex];
      trigger.textContent = selectedOption ? selectedOption.textContent : 'Select option';
      Array.from(menu.children).forEach((node) => {
        if(!(node instanceof HTMLElement)) return;
        node.classList.toggle('active', node.dataset.value === selectEl.value && node.dataset.value !== '');
      });
    }

    function rebuildOptions(){
      menu.innerHTML = '';
      const options = Array.from(selectEl.options || []);
      options.forEach((option) => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'tbmCustomSelectOption';
        item.textContent = option.textContent || '';
        item.dataset.value = option.value;
        item.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          selectEl.value = option.value;
          selectEl.dispatchEvent(new Event('change', { bubbles: true }));
          closeMenu();
        });
        menu.appendChild(item);
      });
      syncTrigger();
    }

    trigger.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      if(menu.classList.contains('open')){
        closeMenu();
      } else {
        openMenu();
      }
    });

    menu.addEventListener('click', (event) => event.stopPropagation());
    menu.addEventListener('mousedown', (event) => event.stopPropagation());

    document.addEventListener('click', (event) => {
      const target = event.target;
      if(target instanceof Node && container.contains(target)) return;
      closeMenu();
    });
    document.addEventListener('keydown', (event) => {
      if(event.key === 'Escape') closeMenu();
    });

    selectEl.addEventListener('change', syncTrigger);

    container.appendChild(trigger);
    container.appendChild(menu);
    selectEl.insertAdjacentElement('afterend', container);

    const idSuffix = Math.random().toString(36).slice(2, 8);
    const triggerId = `${selectEl.id || 'select'}CustomTrigger${idSuffix}`;
    trigger.id = triggerId;
    selectEl.dataset.customSelectTriggerId = triggerId;

    const observer = new MutationObserver(() => rebuildOptions());
    observer.observe(selectEl, { childList: true, subtree: true });

    rebuildOptions();
    return { trigger, menu };
  }

  function enhanceSelects(root = document){
    if(!(root instanceof Document || root instanceof HTMLElement)) return;
    root.querySelectorAll('select.js-custom-select').forEach((selectEl) => enhanceSelect(selectEl));
  }

  window.TBMReservationUI = Object.assign({}, window.TBMReservationUI || {}, {
    splitIsoToDateTime,
    formatLocalIsoDisplay,
    toQuarterHour,
    initQuarterHourSelect,
    setupAirportAutocomplete,
    apiAction,
    enhanceSelect,
    enhanceSelects,
  });
})();
