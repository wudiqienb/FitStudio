/* Opt-in controller. This module never sends requests or infers business results. */
(()=>{'use strict';
const reduced=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
function toast(message,level='info'){
 const host=document.querySelector('[data-fi-toasts]'); if(!host)return;
 const existing=[...host.children].find(item=>item.dataset.level===level&&item.firstElementChild?.textContent===message);if(existing)return existing;
 const item=document.createElement('div'); item.className='fi-toast';item.dataset.level=level;
 item.setAttribute('role',['error','warning'].includes(level)?'alert':'status');
 const text=document.createElement('span');text.textContent=message;
 const close=document.createElement('button');close.type='button';close.textContent='×';close.setAttribute('aria-label','关闭提示');close.onclick=()=>item.remove();
 item.append(text,close);host.append(item);return item;
}
function button(element){
 if(element.fiController)return element.fiController;
 const label=document.createElement('span');label.className='fi-label';while(element.firstChild)label.append(element.firstChild);element.append(label);
 const spinner=document.createElement('span');spinner.className='fi-indicator fi-spinner';spinner.setAttribute('aria-hidden','true');element.append(spinner);
 const check=document.createElement('span');check.classList.add('fi-indicator','fi-check');
 check.setAttribute('aria-hidden','true');check.textContent='✓';element.append(check);
 element.classList.add('fi-button');let busy=false, timer, feedback=document.createElement('div');feedback.className='fi-feedback';feedback.setAttribute('role','status');element.after(feedback);
 const originalLabel=element.getAttribute('aria-label')||label.textContent.trim();
 function state(value){element.dataset.state=value;element.setAttribute('aria-busy',String(value==='loading'));element.setAttribute('aria-disabled',String(busy));element.setAttribute('aria-label',value==='loading'?'正在提交':value==='success'?'操作成功':originalLabel);}
 function cleanup(){clearTimeout(timer);element.querySelectorAll('.fi-particle').forEach(p=>p.remove());}
 const api={get busy(){return busy;},begin(){if(busy)return false;cleanup();feedback.textContent='';busy=true;state('loading');return true;},
 resolve(result){if(!busy||element.dataset.state!=='loading')return false;
 if(!result||typeof result.ok!=='boolean'||typeof result.message!=='string')throw new TypeError('需要服务端明确的 ok 与 message');
 feedback.dataset.level=result.ok?'success':'error';feedback.textContent=result.message;toast(result.message,result.ok?'success':'error');
 if(!result.ok){busy=false;state('error');return true;}state('success');
 if(!reduced()&&!element.hasAttribute('data-fi-neutral'))timer=setTimeout(()=>{for(let i=0;i<10;i++){const p=document.createElement('i');p.className='fi-particle';p.setAttribute('aria-hidden','true');const a=i*Math.PI/5;p.style.setProperty('--dx',Math.cos(a)*39+'px');p.style.setProperty('--dy',Math.sin(a)*39+'px');element.append(p);}timer=setTimeout(()=>element.querySelectorAll('.fi-particle').forEach(p=>p.remove()),400);},260+(element.hasAttribute('data-fi-contract-on-success')?220:0));
 return true;},fail(message='网络连接失败，请检查操作结果后重试。'){return api.resolve({ok:false,message});},reset(){cleanup();busy=false;feedback.textContent='';state('idle');}};
 element.addEventListener('click',e=>{if(busy){e.preventDefault();e.stopImmediatePropagation();}},true);
 element.addEventListener('pointerdown',()=>{if(!busy)state('pressed');});
 ['pointerup','pointercancel','blur'].forEach(event=>element.addEventListener(event,()=>{if(!busy&&element.dataset.state==='pressed')state('idle');}));
 state('idle');element.fiController=api;return api;
}
async function requestJSON(url,options){
 const controller=new AbortController();let timer;
 const timeout=new Promise((resolve,reject)=>{timer=setTimeout(()=>{controller.abort();const error=new Error('请求超时，请检查操作结果后重试。');error.name='TimeoutError';reject(error);},20000);});
 try{return await Promise.race([(async()=>{const response=await fetch(url,{...options,signal:controller.signal});if(!response.headers.get('content-type')?.includes('application/json'))throw Object.assign(new Error('Unexpected response'),{status:response.status});const result=await response.json();return {response,result};})(),timeout]);}finally{clearTimeout(timer);}
}
window.FitStudioInteraction=Object.freeze({button,toast,requestJSON});
document.querySelectorAll('[data-fi-button]').forEach(button);
document.querySelectorAll('[data-fi-coach-form]').forEach(form=>{
 const el=form.querySelector('[data-fi-button]');if(!el||!window.fetch||!window.FormData)return;
 const api=button(el);let confirmed=false;
 form.addEventListener('submit',async event=>{
  event.preventDefault();if(api.busy)return;
  if(form.hasAttribute('data-fi-cautious')&&!confirmed){if(!window.confirm('请核对页面上的操作说明、课时影响和原因，确认执行？'))return;confirmed=true;}
  if(!api.begin())return;
  const loadingStarted=performance.now();
  const fields=new FormData(form);if(event.submitter&&event.submitter.name)fields.append(event.submitter.name,event.submitter.value);
  try{
   const {response,result}=await requestJSON(form.action,{method:'POST',body:fields,credentials:'same-origin',headers:{'X-FitStudio-Interaction':'1','Accept':'application/json'}});
   if(!response.ok&&result.ok)throw new Error('Unexpected response');
   if(result.ok&&!reduced())await new Promise(resolve=>setTimeout(resolve,Math.max(0,220-(performance.now()-loadingStarted))));
   api.resolve(result);
   if(result.ok){form.setAttribute('data-fi-complete','');const region=document.querySelector('[data-fi-result]');if(region&&typeof result.html==='string')region.innerHTML=result.html;form.querySelectorAll('input,select,textarea').forEach(field=>field.disabled=true);}
   else confirmed=false;
  }catch(error){api.fail(error.name==='TimeoutError'?'请求超时，请检查操作结果后重试。':error.status===403?'权限或安全校验失败，请刷新后重试。':undefined);}
 });
});
document.querySelectorAll('[data-fi-message]').forEach(node=>{toast(node.textContent.trim(),node.dataset.level);node.hidden=true;});
addEventListener('pageshow',event=>{if(event.persisted)document.querySelectorAll('[data-fi-button]').forEach(el=>{if(!el.closest('[data-fi-complete]'))el.fiController.reset();});});
})();
