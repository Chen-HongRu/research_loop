'use strict';

// TensorBoard 2.21.0 reads these preferences before starting its browser timer.
// Initialize only an absent choice; an explicit user setting always wins.
(() => {
  try {
    const storage=window.localStorage, key='_tb_global_settings';
    const read=name=>{
      const value=storage.getItem(name);
      if(value===null)return {};
      const parsed=JSON.parse(value);
      if(!parsed || typeof parsed!=='object' || Array.isArray(parsed))throw new Error('Invalid settings');
      return parsed;
    };
    const legacy=read('_tb_global_settings.timeseries'), current=read(key);
    if(!Object.hasOwn(legacy,'autoReload') && !Object.hasOwn(current,'autoReload'))storage.setItem(key,JSON.stringify({...current,autoReload:true}));
  } catch (_) {
    // Disabled storage or malformed user settings do not block TensorBoard.
  }
})();
