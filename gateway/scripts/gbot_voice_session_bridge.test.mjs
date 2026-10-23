// SPDX-FileCopyrightText: 2026 Joseph Zhou
// SPDX-License-Identifier: MIT
import test from 'node:test';
import assert from 'node:assert/strict';
import { createBridge, accountRpc, validateBody } from './gbot_voice_session_bridge.mjs';
import { createServer } from 'node:net';
import { request } from 'node:http';

async function freePort() {
  const s = createServer(); await new Promise(r => s.listen(0, '127.0.0.1', r));
  const port = s.address().port; await new Promise(r => s.close(r)); return port;
}

test('loopback bridge rejects cross origin, wrong host, unknown RPC without consulting login', async () => {
  let calls = 0; const port = await freePort();
  const server = createBridge({port, agentId: 'a', rpc: async (method, body) => { calls++; return { instructions: 'test', method, body }; }});
  await new Promise(r => server.listen(port, '127.0.0.1', r));
  try {
    const url = `http://127.0.0.1:${port}`;
    assert.equal((await fetch(url + '/credential', {headers:{Origin:'https://example.com'}})).status, 403);
    const wrongHostStatus = await new Promise((resolve, reject) => { const req = request(url + '/credential', {headers:{Host:`localhost:${port}`}}, res => {res.resume(); resolve(res.statusCode);}); req.on('error', reject); req.end(); });
    assert.equal(wrongHostStatus, 403);
    assert.equal((await fetch(url + '/api/sendPrompt', {method:'POST'})).status, 404);
    assert.equal((await fetch(url + '/session', {method:'POST',headers:{'Content-Type':'application/json'},body:'{"agentId":"a","agentName":"b","rpc":"DeleteAgent"}'})).status, 400);
    assert.equal((await fetch(url + '/tool', {method:'POST'})).status, 403);
    assert.equal((await fetch(url + '/session', {method:'POST',headers:{'Content-Type':'application/json'},body:'{"agentId":"other","agentName":"b"}'})).status, 403);
    assert.equal(calls, 0);
    const response = await fetch(url + '/session', {method:'POST',headers:{'Content-Type':'application/json'},body:'{"agentId":"a","agentName":"b"}'});
    assert.equal(response.status, 200); assert.equal((await response.json()).method, 'VoiceCallHarnessSession'); assert.equal(calls, 1);
  } finally { await new Promise(r => server.close(r)); }
});

test('account RPC admits only fixed methods and never leaks upstream response body', async () => {
  let loads = 0; const accountLoader = () => {loads++; return {token:'FAKE_TEST',machineId:'FAKE_MACHINE'};};
  await assert.rejects(accountRpc('SendPrompt', {}, {accountLoader}), /rpc_forbidden/); assert.equal(loads, 0);
  const fetcher = async (url, options) => {
    assert.equal(url, 'https://api2.cursor.sh/aiserver.v1.GrokBotService/MintSandVoiceCallSecret');
    assert.equal(options.redirect, 'error'); return {ok:false,status:403,json:async()=>({token:'DO_NOT_LEAK'})};
  };
  await assert.rejects(accountRpc('MintSandVoiceCallSecret', {}, {accountLoader,fetcher}), /^Error: upstream_http_403$/);
});

test('tool invocation requires complete bounded line state', () => {
  assert.throws(() => validateBody('POST /tool', {agentId:'a',callId:'c',name:'x',input:{},line:{}}), /invalid_line/);
});

test('native bootstrap uses fixed read-only request and cancels bodies without reading snapshots', async () => {
  const {NativeSandboxClient, validateSandboxUrl} = await import('./gbot_voice_session_bridge.mjs');
  for (const url of ['http://a.cursor.sh', 'https://cursor.sh.evil.test', 'https://u@a.cursor.sh', 'https://a.cursor.sh/?token=x']) assert.throws(() => validateSandboxUrl(url));
  let cancelled = 0; const paths = [];
  const client = new NativeSandboxClient({accountLoader:()=>({authId:'owner'}), rpc:async(method,body)=>{
    assert.equal(method,'EnsureSandBox'); assert.deepEqual(body,{wake:false,supportsAsleepBox:true});
    return {gatewayUrl:'https://pod.cursorvm.com',gatewayToken:'FAKE',networkToken:'FAKE_NETWORK'};
  },fetcher:async(url,options)=>{
    paths.push(new URL(url).pathname); assert.equal(options.redirect,'error');
    return {ok:true,status:200,headers:new Headers({'content-type':'text/event-stream'}),body:{cancel:async()=>{cancelled++;}},json:()=>assert.fail('must not read body')};
  }});
  // connect verifies /health once before the connection is cached, then probe checks it again
  const result = await client.probe(); assert.equal(result.ok,true); assert.deepEqual(paths,['/health','/health','/events']); assert.equal(cancelled,3);
  assert.equal(JSON.stringify(result).includes('FAKE'),false);
});

test('native event filtering excludes history, other callers, sessions and duplicate entries', async () => {
  const {filterNativeEvent} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a',callId:'call-c',sessionId:'',authId:'owner',sinceMs:100,seen:new Set()};
  const entry = {id:'e',kind:'message',role:'user',content:'FAKE TEXT',timestampMs:101,author:{kind:'cursor_user',id:'owner'}};
  const event = {channel:'transcript',payload:{type:'appended',entry,ordered:{replicaKey:'transcript:a',sequence:1}}};
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,type:'snapshot'}},context),null);
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,entry:{...entry,author:{kind:'cursor_user',id:'other'}}}},context),null);
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,entry:{...entry,timestampMs:99}}},context),null);
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,ordered:{replicaKey:'transcript:a\0other',sequence:1}}},context),null);
  assert.equal(filterNativeEvent(event,context).channel,'typed-message'); assert.equal(filterNativeEvent(event,context),null);
  assert.equal(filterNativeEvent({channel:'voice-call-nudge',payload:{agentId:'a',callId:'other',kind:'outcome',update:'fake'}},context),null);
});

test('desktop opt-in admits native send and search scoped to active call while unknown tools remain off', async () => {
  const port = await freePort(); let toolCalls=0, presenceCalls=0;
  const sandbox={callerAuthId:async()=> 'owner', presence:async()=>{presenceCalls++;}};
  const server=createBridge({port,agentId:'a',desktopEnabled:true,toolsEnabled:false,sandbox,rpc:async()=>{toolCalls++;return {kind:1};}});
  await new Promise(r=>server.listen(port,'127.0.0.1',r));
  const post=async(path,body)=>fetch(`http://127.0.0.1:${port}`+path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  const tool={agentId:'a',callId:'call-c',name:'send_task',input:{request:'FAKE'},line:{theyJustTalked:false,newsOwed:false,receiptOwed:false,alreadySpokeThisTurn:false}};
  try {
    assert.equal((await post('/tool',tool)).status,403);
    assert.equal((await post('/native/presence',{agentId:'a',callId:'call-c',isOnTheLine:true,acceptsOverheard:false})).status,200);
    assert.equal((await post('/tool',{...tool,name:'unknown_native_tool'})).status,403);
    assert.equal((await post('/tool',{...tool,name:'search_conversations',input:{query:'fixture'}})).status,200);
    assert.equal((await post('/tool',{...tool,callId:'other'})).status,403);
    assert.equal((await post('/tool',tool)).status,200); assert.equal(toolCalls,2);
    assert.equal((await post('/native/presence',{agentId:'a',callId:'call-c',isOnTheLine:false,acceptsOverheard:false})).status,200);
    assert.equal((await post('/tool',tool)).status,403); assert.equal(presenceCalls,2);
  } finally {await new Promise(r=>server.close(r));}
});

test('server transcript stream drops replay and pre-connection entries and requires rebaseline after clear', async()=>{
  const {filterTranscriptFrame,NativeTranscriptClient}=await import('./gbot_voice_session_bridge.mjs');
  const context={agentId:'a',callId:'call-c',sessionId:'',authId:'owner',sinceMs:100,seen:new Set()};
  const state={};
  assert.deepEqual(filterTranscriptFrame({connected:{serverTimeMs:'110'}},context,state),[]);
  const row=(time,id='e')=>({body:Buffer.from(JSON.stringify({id,kind:'message',role:'user',content:'FAKE',timestampMs:time,author:{kind:'cursor_user',id:'owner'}})).toString('base64')});
  const rows={agentId:'a',sessionId:'',generation:1,entries:[row(120)]};
  assert.deepEqual(filterTranscriptFrame({rows:{...rows,replay:true}},context,state),[]);
  assert.deepEqual(filterTranscriptFrame({rows:{...rows,entries:[row(109)]}},context,state),[]);
  assert.equal(filterTranscriptFrame({rows},context,state).length,1);
  assert.deepEqual(filterTranscriptFrame({rows},context,state),[]);
  assert.throws(()=>filterTranscriptFrame({cleared:{agentId:'a',newGeneration:2}},context,state),/rebaseline/);
  assert.deepEqual(filterTranscriptFrame({rows:{...rows,entries:[row(130,'e2')]}},context,state),[]);
  let fetched=false;
  const client=new NativeTranscriptClient({fetcher:async()=>{fetched=true;},accountLoader:()=>({})});
  await assert.rejects(async()=>{for await(const ignored of client.events({...context,sessionId:'unverified'})) {}},/session_binding/);
  assert.equal(fetched,false);
});

test('separate native results with identical text are both delivered without event IDs', async () => {
  const {filterNativeEvent} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a',callId:'call-c',seen:new Set()};
  const event = {channel:'voice-call-nudge',payload:{agentId:'a',callId:'call-c',kind:'outcome',update:'SAME FIXTURE RESULT'}};
  assert.equal(filterNativeEvent(event,context)?.channel,'voice-call-nudge');
  assert.equal(filterNativeEvent(event,context)?.channel,'voice-call-nudge');
});

test('native event IDs deduplicate replays without conflating separate same-text results', async () => {
  const {filterNativeEvent} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a',callId:'call-c',seen:new Set()};
  const event = {eventId:'event-1',channel:'voice-call-nudge',payload:{agentId:'a',callId:'call-c',kind:'outcome',update:'SAME FIXTURE RESULT'}};
  assert.ok(filterNativeEvent(event,context));
  assert.equal(filterNativeEvent(event,context),null);
  assert.ok(filterNativeEvent({...event,eventId:'event-2'},context));
});

test('HTTP: typed EOF or error reconnects independently and box results still arrive', async () => {
  for (const failure of ['eof', 'error']) {
    const port = await freePort();
    let box, attempts = 0, typedCancelled = false;
    const logs = [];
    const sandbox = {
      presence: async () => {}, callerAuthId: async () => 'owner',
      openEvents: async signal => new Response(new ReadableStream({start(controller) {
        box = controller;
        signal.addEventListener('abort', () => controller.close(), {once:true});
      }}), {headers:{'content-type':'text/event-stream'}}),
    };
    const transcript = {async *events(_context, signal, onOpen) {
      attempts++; onOpen(200);
      if (attempts === 1) {
        if (failure === 'error') throw new TypeError('SECRET_FIXTURE');
        return;
      }
      await new Promise(resolve => signal.addEventListener('abort', resolve, {once:true}));
      typedCancelled = true;
    }};
    const server = createBridge({port, agentId:'a', desktopEnabled:true, toolsEnabled:false, sandbox, transcript, nativeRetryDelayMs:10, nativeLog:event=>logs.push(event)});
    await new Promise(resolve=>server.listen(port,'127.0.0.1',resolve));
    let reader;
    try {
      await fetch(`http://127.0.0.1:${port}/native/presence`, {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({agentId:'a',callId:'call-c',isOnTheLine:true,acceptsOverheard:false})});
      const response=await fetch(`http://127.0.0.1:${port}/native/events?agentId=a&callId=call-c`);
      reader=response.body.getReader(); await reader.read();
      await new Promise(resolve=>setTimeout(resolve,30));
      assert.ok(attempts>=2);
      box.enqueue(new TextEncoder().encode('data: '+JSON.stringify({channel:'voice-call-nudge',payload:{agentId:'a',callId:'call-c',kind:'outcome',update:'FIXTURE RESULT'}})+'\n\n'));
      const result=await reader.read();
      assert.equal(result.done,false); assert.match(new TextDecoder().decode(result.value),/FIXTURE RESULT/);
      assert.ok(logs.some(event=>event.source==='typed'&&event.state===failure));
      assert.equal(JSON.stringify(logs).includes('SECRET_FIXTURE'),false);
      await reader.cancel(); reader=null;
      await new Promise(resolve=>setTimeout(resolve,20));
      assert.equal(typedCancelled,true);
    } finally {await reader?.cancel();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
  }
});

test('agent transcript replies relay as call outcomes only when tagged with the call, fresh and new', async () => {
  const {filterNativeEvent, filterTranscriptFrame} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a',callId:'call-c',sessionId:'',authId:'owner',sinceMs:100,seen:new Set()};
  const reply = {id:'r1',kind:'send-message',message:{type:'text',content:'FAKE RESULT',channel:'voice:call-c'},timestampMs:150,requestId:'req'};
  const wrap = entry => ({channel:'transcript',payload:{type:'appended',entry,ordered:{replicaKey:'transcript:a',sequence:1}}});
  const relayed = filterNativeEvent(wrap(reply), context);
  assert.deepEqual(relayed, {channel:'voice-call-nudge',payload:{agentId:'a',callId:'call-c',kind:'outcome',update:'FAKE RESULT'}});
  assert.equal(filterNativeEvent(wrap(reply), context), null, 'same entry id is not relayed twice');
  assert.equal(filterNativeEvent(wrap({...reply,id:'r2',timestampMs:99}), context), null, 'older than the call');
  assert.equal(filterNativeEvent(wrap({...reply,id:'r3',message:{...reply.message,channel:'voice:other'}}), context), null, 'another call');
  assert.equal(filterNativeEvent(wrap({...reply,id:'r4',message:{...reply.message,content:'   '}}), context), null, 'blank');
  // Frames without rows.generation (Grok Bot 0.72.0) still deliver entries.
  const state = {connected:false,sinceMs:100,generation:null};
  filterTranscriptFrame({connected:{serverTimeMs:'100'}}, context, state);
  const row = {body: Buffer.from(JSON.stringify({...reply,id:'r5'})).toString('base64')};
  assert.equal(filterTranscriptFrame({rows:{agentId:'a',replay:false,entries:[row]}}, context, state).length, 1);
});

test('the same result from the box nudge and the transcript reply is relayed once within 15 s', async () => {
  const {filterNativeEvent} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a',callId:'call-c',sessionId:'',authId:'owner',sinceMs:100,seen:new Set(),nowMs:1000000};
  const nudge = {channel:'voice-call-nudge',payload:{agentId:'a',callId:'call-c',kind:'outcome',update:'SAME RESULT'}};
  const reply = {channel:'transcript',payload:{type:'appended',entry:{id:'r9',kind:'send-message',message:{type:'text',content:'SAME RESULT',channel:'voice:call-c'},timestampMs:150},ordered:{replicaKey:'transcript:a',sequence:1}}};
  assert.equal(filterNativeEvent(nudge, context).payload.update, 'SAME RESULT');
  assert.equal(filterNativeEvent(reply, context), null, 'transcript copy 0 ms later is a duplicate');
  context.nowMs += 20000;
  assert.equal(filterNativeEvent({...reply,payload:{...reply.payload,entry:{...reply.payload.entry,id:'r10'}}}, context).payload.update, 'SAME RESULT', 'after the window equal text is a new result');
  const other = {...nudge,payload:{...nudge.payload,update:'OTHER'}};
  assert.equal(filterNativeEvent(other, context).payload.update, 'OTHER');
  assert.equal(filterNativeEvent(other, context).payload.update, 'OTHER', 'two box outcomes with equal text stay two results');
});

test('a new call takes over a stale active call that has no event stream, but not a live one', async () => {
  const port = await freePort(); const presences=[]; let reader;
  const sandbox={callerAuthId:async()=> 'owner', presence:async(body)=>{presences.push([body.callId, body.isOnTheLine]); if (body.callId==='call-old' && !body.isOnTheLine) throw new Error('upstream_http_404');},
    openEvents: async signal => new Response(new ReadableStream({start(controller) { signal.addEventListener('abort', () => controller.close(), {once:true}); }}), {headers:{'content-type':'text/event-stream'}})};
  const transcript={async *events(_context, signal) { await new Promise(resolve => signal.addEventListener('abort', resolve, {once:true})); }};
  const server=createBridge({port,agentId:'a',desktopEnabled:true,toolsEnabled:false,sandbox,transcript,rpc:async()=>({kind:1}),nativeLog:()=>{}});
  await new Promise(r=>server.listen(port,'127.0.0.1',r));
  const post=async(path,body)=>fetch(`http://127.0.0.1:${port}`+path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  try {
    assert.equal((await post('/native/presence',{agentId:'a',callId:'call-old',isOnTheLine:true,acceptsOverheard:false})).status,200);
    // Going off the line for a different call never touches the active one.
    assert.equal((await post('/native/presence',{agentId:'a',callId:'call-new',isOnTheLine:false,acceptsOverheard:false})).status,409);
    // No event stream was ever attached to call-old: the owner is gone, so call-new takes over,
    // even though the box no longer knows call-old (its presence-off answers 404).
    assert.equal((await post('/native/presence',{agentId:'a',callId:'call-new',isOnTheLine:true,acceptsOverheard:false})).status,200);
    assert.deepEqual(presences,[['call-old',true],['call-old',false],['call-new',true]]);
    const tool={agentId:'a',callId:'call-new',name:'send_task',input:{request:'FAKE'},line:{theyJustTalked:false,newsOwed:false,receiptOwed:false,alreadySpokeThisTurn:false}};
    assert.equal((await post('/tool',tool)).status,200);
    assert.equal((await post('/tool',{...tool,callId:'call-old'})).status,403);
    // With a live event stream the owner is alive: another call must still wait.
    const response=await fetch(`http://127.0.0.1:${port}/native/events?agentId=a&callId=call-new`);
    reader=response.body.getReader(); await reader.read();
    assert.equal((await post('/native/presence',{agentId:'a',callId:'call-x',isOnTheLine:true,acceptsOverheard:false})).status,409);
    await reader.cancel(); reader=null;
    await new Promise(resolve=>setTimeout(resolve,20));
    assert.equal((await post('/native/presence',{agentId:'a',callId:'call-x',isOnTheLine:true,acceptsOverheard:false})).status,200);
  } finally {await reader?.cancel();server.closeAllConnections();await new Promise(r=>server.close(r));}
});

test('sandbox client waits for a waking box, reconnects after a 5xx and keeps the box awake', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  const rpcs=[]; const paths=[]; const logs=[];
  let healthFailures=1, tokens=0;
  const client = new NativeSandboxClient({connectMode:'normal', wakePollMs:1, wakeTimeoutMs:5000, log:(source,state,extra)=>logs.push([source,state,extra]),
    accountLoader:()=>({authId:'owner'}),
    rpc:async(method,body)=>{
      assert.equal(method,'EnsureSandBox'); assert.deepEqual(body,{});
      rpcs.push(1);
      const runState = rpcs.length < 3 ? 'SAND_BOX_RUN_STATE_WAKING' : 'SAND_BOX_RUN_STATE_RUNNING';
      return {gatewayUrl:'https://pod.cursorvm.com', gatewayToken:'FAKE'+(++tokens), runState};
    },
    fetcher:async(url,options)=>{
      const path=new URL(url).pathname; paths.push([path, options.headers.authorization]);
      if (path==='/health' && healthFailures>0) { healthFailures--; return {ok:false,status:503,body:{cancel:async()=>{}}}; }
      if (path==='/api/setVoiceCallPresence' && options.headers.authorization==='Bearer FAKE3') return {ok:false,status:417,body:{cancel:async()=>{}}};
      return {ok:true,status:200,headers:new Headers({'content-type':'text/event-stream'}),body:{cancel:async()=>{}}};
    }});
  await client.presence({agentId:'a',callId:'c',isOnTheLine:true,acceptsOverheard:false});
  // two WAKING answers, then RUNNING; /health 503 once, then ok; the presence call itself got a 502
  // on the first connection (token FAKE3, a 417 as the box answers while coming up) and succeeded after a reconnect (token FAKE4).
  assert.equal(rpcs.length,4);
  assert.deepEqual(paths.map(p=>p[0]),['/health','/health','/api/setVoiceCallPresence','/health','/api/setVoiceCallPresence']);
  assert.equal(paths.at(-1)[1],'Bearer FAKE4');
  assert.deepEqual(logs.map(l=>l[1]),['waking','awake','reconnect']);
  await client.keepAwake();
  assert.equal(paths.at(-1)[0],'/health'); assert.equal(rpcs.length,4);  // cached connection, no new wake
});

