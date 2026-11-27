// Only this extension's newly created Studio tab is controlled. Existing tabs
// and sign-ins are never closed, copied, reset or modified by the uploader.
const BASE = 'http://127.0.0.1:18761';
let running = false;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function api(path, data) {
  const {token} = await chrome.storage.local.get('token');
  if (!token) throw new Error('Pair the local queue first');
  const response = await fetch(BASE + path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: data === undefined ? undefined : JSON.stringify(data),
    signal: AbortSignal.timeout(30000), cache: 'no-store'
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Local queue unavailable');
  return result;
}

async function note(status) {
  await chrome.storage.local.set({status, updated: Date.now()});
}

async function trustedClick(tabId, point) {
  if(!point || !Number.isFinite(point.x) || !Number.isFinite(point.y))return false;
  const target={tabId};
  await chrome.debugger.sendCommand(target,'Input.dispatchMouseEvent',{type:'mouseMoved',x:point.x,y:point.y});
  await chrome.debugger.sendCommand(target,'Input.dispatchMouseEvent',{type:'mousePressed',x:point.x,y:point.y,button:'left',clickCount:1});
  await chrome.debugger.sendCommand(target,'Input.dispatchMouseEvent',{type:'mouseReleased',x:point.x,y:point.y,button:'left',clickCount:1});
  return true;
}

async function clickFrom(tabId, func, args=[]) {
  const [r]=await chrome.scripting.executeScript({target:{tabId},func,args});
  return await trustedClick(tabId,r?.result);
}

// Runs in the owned Studio tab. Deliberately supports known labels only and
// refuses ambiguous controls instead of clicking by screen coordinates.
async function pageAction(tabId, action, value) {
  const [result] = await chrome.scripting.executeScript({
    target: {tabId}, args: [action, value ?? null],
    func: (action, value) => {
      const visible = el => !!el && !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
      const text = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      const controls = () => [...document.querySelectorAll('button, ytcp-button, [role="tab"], [role="radio"], tp-yt-paper-radio-button, ytcp-text-menu')].filter(visible);
      const find = names => controls().filter(el => names.includes(el.getAttribute('aria-label')) || names.includes(text(el)));
      const click = names => {
        let found = find(names);
        // Polymer wraps the same inner button: retain the innermost control.
        found = found.filter(el => !found.some(other => other !== el && el.contains(other)));
        if (found.length !== 1) return false;
        if (found[0].disabled || found[0].getAttribute('aria-disabled') === 'true') return false;
        const r=found[0].getBoundingClientRect();
        return {click:{x:r.x+r.width/2,y:r.y+r.height/2}};
      };
      const identity = () => {
        const home = document.querySelector('a#home-button');
        const href = home?.getAttribute('href') || '';
        return href.match(/\/channel\/(UC[\w-]{22})(?:[/?]|$)/)?.[1] || null;
      };
      if (action === 'identity') return identity();
      if (action === 'click') return click(value);
      if (action === 'fileReady') return !!document.querySelector('input[type="file"]');
      if (action === 'fill') {
        const editors = [...document.querySelectorAll('[contenteditable="true"][role="textbox"],input,textarea')]
          .filter(el => visible(el) && el.getAttribute('aria-label') === value.label);
        if (editors.length !== 1) return false;
        const r=editors[0].getBoundingClientRect();
        return {edit:{x:r.x+r.width/2,y:r.y+r.height/2,text:value.text,label:value.label}};
      }
      if (action === 'checks') {
        const nodes = [...document.querySelectorAll('h1,h2,h3,ytcp-video-upload-progress,ytcp-uploads-checks,ytcp-ve,span,div')].filter(visible);
        const labels = nodes.map(text);
        return labels.includes('Checks complete. No issues found.') ||
          (labels.includes('Initial check') && nodes.filter(el => text(el) === 'No issues found' && ![...el.children].some(child => text(child) === 'No issues found')).length === 2);
      }
      if (action === 'publicSelected') {
        return controls().some(el => (text(el) === 'Public' || el.getAttribute('aria-label') === 'Public') &&
          (el.getAttribute('aria-checked') === 'true' || el.hasAttribute('checked')));
      }
      if (action === 'videoLink') return document.querySelector('a#video-link')?.href || '';
      if (action === 'published') return [...document.querySelectorAll('[role="heading"],h1,h2,h3')].some(el => visible(el) && text(el) === 'Video published');
      throw new Error('Unknown Studio action');
    }
  });
  const response=result?.result;
  if(response?.click)return await trustedClick(tabId,response.click);
  if(response?.edit) {
    await trustedClick(tabId,response.edit);
    await chrome.debugger.sendCommand({tabId},'Input.dispatchKeyEvent',{type:'keyDown',key:'a',code:'KeyA',windowsVirtualKeyCode:65,modifiers:2});
    await chrome.debugger.sendCommand({tabId},'Input.dispatchKeyEvent',{type:'keyUp',key:'a',code:'KeyA',windowsVirtualKeyCode:65,modifiers:2});
    await chrome.debugger.sendCommand({tabId},'Input.insertText',{text:response.edit.text});
    const [read]=await chrome.scripting.executeScript({target:{tabId},args:[response.edit.label,response.edit.text],func:(label,expected)=>{
      const fields=[...document.querySelectorAll('[contenteditable="true"][role="textbox"],input,textarea')].filter(e=>e.getClientRects().length&&e.getAttribute('aria-label')===label);
      return fields.length===1&&(fields[0].isContentEditable?fields[0].textContent:fields[0].value).trim()===expected.trim();
    }});
    return !!read?.result;
  }
  return response;
}

async function until(task, label, seconds=60) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    const result = await task();
    if (result) return result;
    await pause(1000);
  }
  throw new Error('Stopped at ' + label + '; inspect the Studio tab');
}

async function identity(tabId, expected) {
  await until(async () => {
    const actual = await pageAction(tabId, 'identity');
    if (actual && actual !== expected) throw new Error('Wrong channel signed in; no clip will be posted');
    return actual === expected;
  }, 'channel verification', 45);
}

async function selectChannel(tabId, job) {
  if (job.channel_label) {
    // The full account menu includes channels under other signed-in Google
    // accounts; /channel_switcher alone lists only the current Google account.
    await chrome.tabs.update(tabId,{url:'https://www.youtube.com/account'});
    await until(async()=>{
      return await clickFrom(tabId,()=>{
        const avatar=document.querySelector('#avatar-btn');
        if(!avatar?.getClientRects().length)return false;
        const r=avatar.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};
      });
    },'YouTube account menu');
    await until(async()=>{
      return await clickFrom(tabId,()=>{
        const links=[...document.querySelectorAll('a')].filter(el=>el.getClientRects().length&&(el.innerText||'').trim()==='Switch account');
        if(links.length!==1)return false;const r=links[0].getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};
      });
    },'Switch account menu');
    await until(async()=>{
      return await clickFrom(tabId,(label)=>{
        const cards=[...document.querySelectorAll('ytd-account-item-renderer')].filter(el=>el.getClientRects().length);
        const matches=cards.filter(el=>(el.innerText||'').split('\n').some(line=>line.trim()===label));
        if(matches.length!==1)return false;
        const option=matches[0].querySelector('[role="option"]');
        if(!option||option.getAttribute('aria-disabled')==='true')return false;
        const r=option.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};
      },[job.channel_label]);
    },'exact signed-in channel name',45);
    await until(async()=>{
      const [r]=await chrome.scripting.executeScript({target:{tabId},args:[job.channel_id],func:expected=>
        [...document.querySelectorAll('a')].some(a=>a.getClientRects().length&&(a.getAttribute('aria-label')==='Your channel'||a.closest('#channel-info'))&&(a.href==='https://www.youtube.com/channel/'+expected||a.href==='https://www.youtube.com/channel/'+expected+'/'))
      });return r?.result;
    },'selected YouTube channel identity',45);
    await chrome.tabs.update(tabId,{url:'https://studio.youtube.com/channel/'+job.channel_id});
    await identity(tabId,job.channel_id);
    return;
  }
  // A saved handle selects a channel in the ordinary picker, but never proves
  // identity: the exact Studio channel ID is still checked after selection.
  if (job.channel_handle) {
    await chrome.tabs.update(tabId,{url:'https://www.youtube.com/channel_switcher?next='+encodeURIComponent('https://studio.youtube.com/channel/'+job.channel_id)});
    await until(async()=>{
      return await clickFrom(tabId,(handle)=>{
        const cards=[...document.querySelectorAll('ytd-account-item-renderer')].filter(el=>el.getClientRects().length);
        const exact=cards.filter(el=>(el.innerText||'').split('\n').some(line=>line.trim().toLowerCase()===handle.toLowerCase()));
        if(exact.length!==1)return false;
        const option=exact[0].querySelector('[role="option"]');
        if(!option||option.getAttribute('aria-disabled')==='true')return false;
        const r=option.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};
      },[job.channel_handle]);
    },'signed-in channel picker',45);
  }
  await identity(tabId,job.channel_id);
}

async function upload(job) {
  // YouTube defers menu hydration in background tabs. Use an owned foreground
  // tab for interactive uploads; leave every pre-existing tab untouched.
  const tab = await chrome.tabs.create({url: 'https://studio.youtube.com/channel/' + job.channel_id, active:true});
  let attached = false, posted = false, heartbeat;
  try {
    await chrome.storage.local.set({activeJob: job.id, activeTab: tab.id});
    // Active debugger sessions keep the service worker alive through long uploads.
    await chrome.debugger.attach({tabId:tab.id}, '1.3'); attached = true;
    heartbeat = setInterval(() => api('/event', {job_id:job.id,event:'heartbeat'}).catch(() => {}), 20000);
    await selectChannel(tab.id, job);
    await until(() => pageAction(tab.id,'click',['Create']), 'Create');
    // Upload videos is a menu item rather than a normal button in some layouts.
    await until(async () => {
      return await clickFrom(tab.id,() => {
        const nodes=[...document.querySelectorAll('[role="menuitem"][test-id="upload"]')]
          .filter(el => el.getClientRects().length && (el.innerText||el.textContent).trim()==='Upload videos');
        if(nodes.length!==1)return false;
        const r=nodes[0].getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};
      });
    }, 'Upload videos');
    await until(() => pageAction(tab.id,'fileReady'), 'file picker');
    await identity(tab.id, job.channel_id);
    await api('/event',{job_id:job.id,event:'start',channel_id:job.channel_id});
    const doc = await chrome.debugger.sendCommand({tabId:tab.id},'DOM.getDocument',{depth:0});
    const node = await chrome.debugger.sendCommand({tabId:tab.id},'DOM.querySelector',{nodeId:doc.root.nodeId,selector:'input[type="file"]'});
    if (!node.nodeId) throw new Error('Studio file input missing');
    await chrome.debugger.sendCommand({tabId:tab.id},'DOM.setFileInputFiles',{nodeId:node.nodeId,files:[job.file]});
    await until(() => pageAction(tab.id,'fill',{label:'Add a title that describes your video (type @ to mention a channel)',text:job.title}),'title',120);
    await until(() => pageAction(tab.id,'fill',{label:'Tell viewers about your video (type @ to mention a channel)',text:job.description}),'description');
    await until(() => pageAction(tab.id,'click',["No, it's not made for kids"]),'audience');
    await until(() => pageAction(tab.id,'click',['Checks','Initial check']),'checks tab');
    await until(() => pageAction(tab.id,'checks'),'YouTube checks',600);
    await until(() => pageAction(tab.id,'click',['Visibility']),'visibility tab');
    await until(() => pageAction(tab.id,'click',['Public']),'public visibility');
    await until(() => pageAction(tab.id,'publicSelected'),'confirmed public visibility');
    const url = await pageAction(tab.id,'videoLink');
    if (!/^https:\/\/(?:www\.)?(?:youtube\.com\/(?:shorts\/|watch\?v=)|youtu\.be\/)[\w-]+$/.test(url)) throw new Error('Missing publication URL');
    await identity(tab.id, job.channel_id);
    // Stop button takes effect before the irreversible publish action too.
    if (!(await chrome.storage.local.get('enabled')).enabled) throw new Error('Posting stopped before Publish');
    await until(() => pageAction(tab.id,'click',['Publish']),'Publish');
    await until(() => pageAction(tab.id,'published'),'publication confirmation',120);
    await api('/event',{job_id:job.id,event:'posted',channel_id:job.channel_id,url});
    posted = true;
    await note('Posted on ' + job.account + ': ' + url);
  } catch (error) {
    await api('/event',{job_id:job.id,event:'failed',detail:String(error.message).slice(0,250)}).catch(() => {});
    await note(job.account + ': ' + error.message);
    // Leave uncertain or failed drafts open for inspection. Never auto-retry them.
  } finally {
    clearInterval(heartbeat);
    if (attached) await chrome.debugger.detach({tabId:tab.id}).catch(()=>{});
    if (posted) await chrome.tabs.remove(tab.id).catch(()=>{});
    await chrome.storage.local.remove(['activeJob','activeTab']);
  }
}

async function tick() {
  if (running) return;
  const {enabled, activeJob} = await chrome.storage.local.get(['enabled','activeJob']);
  if (!enabled) return;
  if (activeJob) { await note('Interrupted upload: inspect its tab before resuming'); return; }
  running = true;
  try {
    const {job,reason} = await api('/next',{});
    if (job) await upload(job);
    else await note(reason);
  } catch(error) { await note(error.message); }
  finally { running = false; }
}
async function alarm() {
  await chrome.alarms.create('clip-queue',{periodInMinutes:1});
}
chrome.runtime.onInstalled.addListener(async()=>{
  // Installing/upgrading never enables public posting automatically.
  await chrome.storage.local.set({enabled:false}); await alarm();
});
chrome.runtime.onStartup.addListener(alarm);
chrome.alarms.onAlarm.addListener(a=>{if(a.name==='clip-queue') tick();});
chrome.runtime.onMessage.addListener((message, sender, reply)=>{
  if (sender.id !== chrome.runtime.id) return;
  (async()=>{
    if(message.type==='status') return await api('/status');
    if(message.type==='tick') { tick(); return {ok:true}; }
    if(message.type==='resume' && typeof message.account==='string') return await api('/resume',{account:message.account});
    if(message.type==='bind' && typeof message.handle==='string') {
      const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
      if(!tab?.url?.startsWith('https://studio.youtube.com/'))throw new Error('Open the intended Studio channel in the active tab first.');
      const channel_id=await pageAction(tab.id,'identity');
      if(!channel_id)throw new Error('Studio channel ID could not be verified.');
      return await api('/bind',{channel_id,handle:message.handle});
    }
    if(message.type==='recover') {
      if(running) throw new Error('An upload is still running. Stop posting and wait for it to finish first.');
      const {activeJob}=await chrome.storage.local.get('activeJob');
      if(activeJob) await api('/event',{job_id:activeJob,event:'failed',detail:'Interrupted upload inspected manually; uncertain clip remains held'}).catch(()=>{});
      await chrome.storage.local.remove(['activeJob','activeTab']);
      return {ok:true};
    }
    throw new Error('Unknown command');
  })().then(reply,error=>reply({error:error.message}));
  return true;
});
