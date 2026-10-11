import {el,button,pageHeader,notify,errorBox,showDetail,jsonView} from '../components/ui.js';
export function mount(root,{store,api}) {
  let schema=null,table='sessions',offset=0,sort='',direction='asc',generation=0,disposed=false;
  const tables=el('select',{'aria-label':'选择数据库表'}),column=el('select',{'aria-label':'筛选列'}),op=el('select',{'aria-label':'筛选操作'},...['eq','contains','is_null'].map(value=>el('option',{value,text:value}))),value=el('input',{'aria-label':'筛选值',placeholder:'筛选值'});
  const size=el('select',{'aria-label':'每页行数'},...['50','100','200'].map(n=>el('option',{value:n,text:`${n} 行 / 页`})));
  pageHeader(root,'Data','只读浏览持久化数据，支持筛选、排序与行详情。',button('刷新',()=>load()));
  const filter=el('form',{class:'filters'},tables,column,op,value,el('button',{type:'submit',text:'筛选'}),size);
  const tableArea=el('div',{class:'data-table card'}), info=el('span',{class:'muted'}),previous=button('← 上一页',()=>{offset=Math.max(0,offset-Number(size.value));load();}),next=button('下一页 →',()=>{offset+=Number(size.value);load();});
  root.append(filter,tableArea,el('div',{class:'pagination'},info,previous,next));
  const updateColumns=()=>{const columns=schema.tables.find(t=>t.name===table).columns;
    column.replaceChildren(el('option',{value:'',text:'所有列'}),...columns.map(c=>el('option',{value:c.name,text:c.name})));sort=columns.find(c=>c.pk)?.name||columns[0].name;};
  tables.addEventListener('change',()=>{table=tables.value;offset=0;direction='asc';updateColumns();load();});
  size.addEventListener('change',()=>{offset=0;load();});op.addEventListener('change',()=>value.disabled=op.value==='is_null');
  filter.addEventListener('submit',e=>{e.preventDefault();offset=0;load();});
  async function load(){if(!schema)return;const gen=++generation,own=table;
    const query={limit:size.value,offset:String(offset),sort,direction};if(column.value)Object.assign(query,{column:column.value,op:op.value,value:value.value});
    tableArea.classList.add('loading');try{const data=await api.request(`/api/data/tables/${own}?${new URLSearchParams(query)}`);
      if(disposed||gen!==generation)return;store.server('data',{...store.get().server.data,[own]:data});render(data);
    }catch(e){if(!disposed&&gen===generation)tableArea.replaceChildren(errorBox(e.message,()=>load()));}
    finally{if(gen===generation)tableArea.classList.remove('loading');}}
  function render(data){const header=el('tr');for(const c of data.columns)header.append(el('th',{},button(`${c.name}${sort===c.name?(direction==='asc'?' ↑':' ↓'):''}`,()=>{direction=sort===c.name&&direction==='asc'?'desc':'asc';sort=c.name;offset=0;load();})));
    const tbody=el('tbody');for(const row of data.rows){const tr=el('tr',{tabindex:0,title:'查看行详情'});
      for(const c of data.columns){const v=row.values[c.name];tr.append(el('td',{text:v==null?'NULL':String(v)+(row.truncated.includes(c.name)?'… [截断]':''),class:v==null?'muted':''}));}
      const detail=async()=>{try{const result=await api.request(`/api/data/tables/${data.table}/row?${new URLSearchParams(row.pk)}`),body=el('div');
        if(result.truncated.length)body.append(el('p',{class:'muted',text:'详情正文总预算 64 KiB；以下标记的字段已截断。'}));
        for(const [key,v] of Object.entries(result.values)){const section=el('section',{class:'row-field'},el('h3',{text:key+(result.truncated.includes(key)?' · 已截断':'')+(result.blobs.includes(key)?' · BLOB 预览':'')}));
          let json;try{json=typeof v==='string'?JSON.parse(v):v;}catch{json=undefined;}
          section.append(json!=null&&typeof json==='object'?jsonView(json):el('pre',{text:v==null?'NULL':String(v)}));body.append(section);}
        showDetail(`${data.table} · 行详情`,body);
      }catch(e){notify(e.message,true);}};
      tr.addEventListener('click',detail);tr.addEventListener('keydown',e=>{if(e.key==='Enter')detail();});tbody.append(tr);
    }
    tableArea.replaceChildren(el('table',{},el('thead',{},header),tbody));if(!data.rows.length)tableArea.append(el('div',{class:'empty',text:'没有匹配的数据。'}));
    info.textContent=`共 ${data.total} 行 · ${data.total?data.offset+1:0}–${data.offset+data.rows.length} · 跨页刷新可能反映新写入`;
    previous.disabled=data.offset===0;next.disabled=data.offset+data.rows.length>=data.total;
  }
  tableArea.append(el('div',{class:'empty',text:'读取表结构…'}));
  api.request('/api/data/schema').then(data=>{if(disposed)return;schema=data;tables.replaceChildren(...data.tables.map(t=>el('option',{value:t.name,text:t.name})));updateColumns();load();}).catch(e=>{if(!disposed)tableArea.replaceChildren(errorBox(e.message));});
  return ()=>{disposed=true;generation++;};
}
