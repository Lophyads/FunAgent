const {chromium}=require(process.argv[2]);
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const [browserPath,url,artifacts]=process.argv.slice(3);
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:browserPath});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const chat=page.locator('#chat'),waitText=(selector,text)=>page.waitForFunction(({selector,text})=>document.querySelector(selector)?.textContent.includes(text),{selector,text},{timeout:8000});
 try{
  await page.goto(url);await page.waitForFunction(()=>document.querySelector('#chat select')?.value);
  const first=await chat.getByLabel('选择会话').inputValue();
  await chat.getByRole('textbox',{name:'消息内容'}).fill('hello stream');await chat.locator('.composer .primary').click();
  await waitText('#chat .messages','discarded');await waitText('#chat .chat-status','运行中');
  await page.reload();await waitText('#chat .messages','discarded');await page.evaluate(()=>fetch('/test/retry'));
  await waitText('#chat .messages','accepted');assert(!(await chat.locator('.messages').textContent()).includes('discarded'));assert.equal(await chat.locator('.messages b').count(),0);
  await page.screenshot({path:path.join(artifacts,'stream-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(artifacts,'stream-mobile.png'),fullPage:true});
  await chat.locator('.chat-header button').click();await page.waitForFunction(first=>document.querySelector('#chat select').value!==first,first);assert.equal(await chat.locator('.streaming').count(),0);
  await chat.getByLabel('选择会话').selectOption(first);await waitText('#chat .messages','accepted');await page.evaluate(()=>fetch('/test/finish'));
  await waitText('#chat .chat-status','完成');await waitText('#chat .messages','accepted');await page.waitForFunction(()=>!document.querySelector('#chat .streaming'));
  const messages=await page.evaluate(async id=>(await(await fetch('/sessions/'+id+'/messages')).json()).items,first);
  assert.deepEqual(messages.map(m=>[m.role,m.content]),[['user','hello stream'],['assistant','accepted <b>literal</b>']]);
  assert.equal(await chat.locator('.message.assistant').count(),1);assert.deepEqual(errors,[]);
  const result={errors,passed:['提交前真实增量','刷新恢复当前尝试','重试隔离失败正文','HTML 转义','会话切换隔离','唯一持久化最终回复','窄屏无溢出']};fs.writeFileSync(path.join(artifacts,'browser-results.json'),JSON.stringify(result,null,2));console.log('Streaming browser smoke passed.');
 }catch(e){await page.screenshot({path:path.join(artifacts,'stream-failure.png'),fullPage:true});throw e;}
 finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
