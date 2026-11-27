const status = document.getElementById('status');
async function refresh() {
  const local=await chrome.storage.local.get(['enabled','status','activeJob']);
  document.getElementById('recover').hidden=!local.activeJob;
  const result=await chrome.runtime.sendMessage({type:'status'});
  if(result.error) {status.textContent=result.error;return;}
  status.textContent=(local.enabled?'Posting ON':'Posting OFF')+'\nTransport: '+result.mode+'\n'+(local.status||'Connected');
  if(result.test_accounts)status.textContent+='\n\nTEST RUN\n'+result.test_accounts.map(a=>a.account+': '+a.state+(a.detail?' — '+a.detail:'')).join('\n');
  const attention=document.getElementById('attention');attention.replaceChildren();
  for(const item of result.attention||[]) {
    const p=document.createElement('p');p.textContent=item.account+': '+item.detail;
    const b=document.createElement('button');b.textContent='I checked Studio — resume '+item.account;
    b.onclick=async()=>{await chrome.runtime.sendMessage({type:'resume',account:item.account});await refresh();};
    attention.append(p,b);
  }
}
document.getElementById('pair').onclick=async()=>{
  const token=document.getElementById('token').value.trim();
  if(token.length<32){status.textContent='Paste the local pairing key from the queue folder.';return;}
  await chrome.storage.local.set({token,enabled:false});document.getElementById('token').value='';await refresh();
};
document.getElementById('start').onclick=async()=>{
  const result=await chrome.runtime.sendMessage({type:'status'});
  if(result.error){status.textContent=result.error;return;}
  await chrome.storage.local.set({enabled:true});await chrome.runtime.sendMessage({type:'tick'});await refresh();
};
document.getElementById('stop').onclick=async()=>{await chrome.storage.local.set({enabled:false});await refresh();};
document.getElementById('bind').onclick=async()=>{
  const handle=document.getElementById('handle').value.trim();
  const result=await chrome.runtime.sendMessage({type:'bind',handle});
  status.textContent=result.error||'Channel handle saved. Its exact channel ID will still be checked before every upload.';
};
document.getElementById('recover').onclick=async()=>{
  const result=await chrome.runtime.sendMessage({type:'recover'});
  if(result.error){status.textContent=result.error;return;}
  await refresh();
};
refresh().catch(e=>status.textContent=e.message);
