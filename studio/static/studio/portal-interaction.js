/* Member form transport; all animation and toast states use the shared controller. */
(()=>{'use strict';
if(!window.FitStudioInteraction||!window.fetch||!window.FormData)return;
const reduced=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
document.querySelectorAll('[data-fi-portal-form]').forEach(form=>{
 const el=form.querySelector('[data-fi-button]');if(!el)return;
 const api=FitStudioInteraction.button(el),region=document.querySelector('[data-fi-portal-result]'),pending=document.querySelector('[data-fi-pending]'),checkbox=form.querySelector('[name=confirmed]');
 function syncConfirmation(){if(checkbox&&!api.busy)el.disabled=!checkbox.checked;}
 if(checkbox){checkbox.addEventListener('change',syncConfirmation);syncConfirmation();}
 function reconfirm(){if(checkbox){checkbox.checked=false;syncConfirmation();}}
 form.addEventListener('submit',async event=>{
  event.preventDefault();if(api.busy||form.hasAttribute('data-fi-complete'))return;
  if(!form.reportValidity()||(checkbox&&!checkbox.checked)){reconfirm();return;}
  if(!api.begin())return;
  if(pending)pending.hidden=false;if(region)region.setAttribute('aria-busy','true');
  const data=new FormData(form);if(event.submitter?.name)data.append(event.submitter.name,event.submitter.value);
  try{
   const {response,result}=await FitStudioInteraction.requestJSON(form.action,{method:'POST',body:data,credentials:'same-origin',headers:{'X-FitStudio-Interaction':'1','Accept':'application/json'}});
   if(!response.ok&&result.ok)throw new Error('Unexpected response');
   api.resolve(result);
   if(result.ok){
    form.setAttribute('data-fi-complete','');form.querySelectorAll('input,select,textarea').forEach(field=>field.disabled=true);
    if(region&&typeof result.html==='string'){region.innerHTML=result.html;region.querySelectorAll('[data-fi-credit]').forEach(number=>number.classList.add('fi-number-update'));}
   }else{
    if(result.confirmation)for(const [name,value] of Object.entries(result.confirmation)){const field=form.elements.namedItem(name);if(field&&field.type==='hidden')field.value=value;}
    for(const [selector,value] of [['[data-fi-rule]',result.rule_text],['[data-fi-booking-summary]',result.booking_summary]]){const node=document.querySelector(selector);if(node&&typeof value==='string')node.textContent=value;}
    const outcome=document.querySelector('[data-fi-outcome]');if(outcome&&typeof result.outcome_text==='string')outcome.textContent=result.outcome_text;
    reconfirm();
   }
  }catch(error){api.fail(error.name==='TimeoutError'?'请求超时，请检查操作结果后重试。':error.status===403?'权限或安全校验失败，请刷新后重试。':undefined);reconfirm();}
  finally{if(pending)pending.hidden=true;if(region)region.setAttribute('aria-busy','false');}
 });
 addEventListener('pageshow',event=>{if(event.persisted&&!form.hasAttribute('data-fi-complete'))reconfirm();});
});
})();
