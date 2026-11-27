const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../chrome-extension-uploader/worker.js'),'utf8');
const calls=[];let elements=[];let home=null;
const noop={addListener(){}};
function element(label,extra={}) {
  return {innerText:label,textContent:label,children:[],disabled:false,
    getClientRects:()=>[{}], getBoundingClientRect:()=>({x:10,y:20,width:100,height:40}),
    getAttribute:name=>name==='aria-label'?label:null,contains:()=>false,hasAttribute:()=>false,
    click(){throw new Error('Synthetic DOM click must not be used');},...extra};
}
const document={querySelectorAll:()=>elements,querySelector:()=>home};
const chrome={
  scripting:{async executeScript({func,args=[]}){return [{result:func(...args)}];}},
  debugger:{async sendCommand(target,method,args){calls.push({target,method,args});
    if(method==='Input.insertText')elements[0].textContent=args.text;
  }},
  runtime:{onInstalled:noop,onStartup:noop,onMessage:noop,id:'test'},
  alarms:{onAlarm:noop},storage:{local:{async get(){return{};}}}
};
const context=vm.createContext({chrome,document,getComputedStyle:()=>({visibility:'visible'}),
  setTimeout,clearTimeout,setInterval,clearInterval,AbortSignal,URL,console});
vm.runInContext(source+'\nglobalThis.subject={pageAction,identity};',context);
(async()=>{
  elements=[element('Publish')];calls.length=0;
  assert.equal(await context.subject.pageAction(7,'click',['Publish']),true);
  assert.deepEqual(calls.map(x=>x.args.type),['mouseMoved','mousePressed','mouseReleased']);
  assert(calls.every(x=>x.target.tabId===7));
  assert.equal(calls[1].args.x,60);assert.equal(calls[1].args.y,40);
  console.log('PASS real mouse input targets the exact tab and control');

  elements=[element('Publish'),element('Publish')];calls.length=0;
  assert.equal(await context.subject.pageAction(7,'click',['Publish']),false);
  assert.equal(calls.length,0);
  console.log('PASS ambiguous publication controls do not click');

  elements=[element('Title',{isContentEditable:true,textContent:'old'})];calls.length=0;
  assert.equal(await context.subject.pageAction(7,'fill',{label:'Title',text:'New title'}),true);
  assert.deepEqual(calls.map(x=>x.method),['Input.dispatchMouseEvent','Input.dispatchMouseEvent','Input.dispatchMouseEvent','Input.dispatchKeyEvent','Input.dispatchKeyEvent','Input.insertText']);
  assert.equal(calls.at(-1).args.text,'New title');
  console.log('PASS editor uses keyboard input and verifies the resulting text');

  home={getAttribute:()=>'/channel/UC'+'b'.repeat(22)};calls.length=0;
  await assert.rejects(context.subject.identity(7,'UC'+'a'.repeat(22)),/Wrong channel/);
  assert.equal(calls.length,0);
  console.log('PASS wrong channel fails before any mouse or file input');
})().catch(e=>{console.error(e);process.exitCode=1;});
