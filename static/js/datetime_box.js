(() => {
  function openPicker(element){
    if(!element) return;
    if(element instanceof HTMLSelectElement){
      const triggerId = element.dataset.customSelectTriggerId;
      if(triggerId){
        const trigger = document.getElementById(triggerId);
        if(trigger){
          trigger.click();
          return;
        }
      }
    }
    if(typeof element.showPicker === 'function'){
      try {
        element.showPicker();
        return;
      } catch (_) {}
    }
    if(typeof element.focus === 'function'){
      element.focus();
    }
  }

  function getInputsForBox(box){
    const dateId = box.getAttribute('data-date-input');
    const timeId = box.getAttribute('data-time-input');
    const dateInput = dateId ? document.getElementById(dateId) : box.querySelector('input[type="date"]');
    const timeInput = timeId ? document.getElementById(timeId) : box.querySelector('input[type="time"], select');
    return { dateInput, timeInput };
  }

  function bindBox(box){
    if(!(box instanceof HTMLElement)) return;
    if(box.dataset.dtBoxBound === '1') return;
    const { dateInput, timeInput } = getInputsForBox(box);
    const dateOk = dateInput instanceof HTMLInputElement;
    const timeOk = (timeInput instanceof HTMLInputElement) || (timeInput instanceof HTMLSelectElement);
    if(!dateOk || !timeOk) return;

    box.addEventListener('click', (event) => {
      const target = event.target;
      if(target instanceof HTMLElement){
        if(target.tagName === 'BUTTON' || target.tagName === 'A') return;
        if(target.tagName === 'LABEL'){
          if(!dateInput.value){
            openPicker(dateInput);
            return;
          }
          openPicker(timeInput);
          return;
        }
        if(target.tagName === 'INPUT' || target.tagName === 'SELECT'){
          openPicker(target);
          return;
        }
      }
      if(!dateInput.value){
        openPicker(dateInput);
        return;
      }
      openPicker(timeInput);
    });

    box.dataset.dtBoxBound = '1';
  }

  function bindAll(root){
    const scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll('[data-dt-box]').forEach(bindBox);
  }

  window.TBMDateTimeBox = {
    bindAll,
    bindBox,
  };

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', () => bindAll(document));
  } else {
    bindAll(document);
  }
})();
