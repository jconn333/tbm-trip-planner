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

  function initQuarterHourSelect(selectEl, placeholder = 'Select time'){
    if(!(selectEl instanceof HTMLSelectElement)) return;
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

  window.TBMReservationUI = Object.assign({}, window.TBMReservationUI || {}, {
    splitIsoToDateTime,
    toQuarterHour,
    initQuarterHourSelect,
    setupAirportAutocomplete,
    apiAction,
  });
})();
