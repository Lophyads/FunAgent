export function trimHistory(history,current=null){
  const size=snapshot=>new TextEncoder().encode(JSON.stringify(snapshot)).length;
  const bytes=()=>history.reduce((sum,step)=>sum+size(step.snapshot),current?size(current):0);
  while(history.length&&(history.length>(current?7:8)||bytes()>8*1024*1024))history.shift();
  return history;
}
export function visibleSelection(selection,visible){return selection&&(selection.kind==='node'?visible.nodes:visible.edges).some(item=>item.id===selection.id)?selection:null;}
export function expired(step,now=Date.now()){return step.stale||now-step.loadedAt>=30000;}
export function requestChannel(){let generation=0,controller;return {cancel(){generation++;controller?.abort();},start(){this.cancel();controller=new AbortController();return {generation,signal:controller.signal};},valid(request){return request.generation===generation&&!request.signal.aborted;}};}
