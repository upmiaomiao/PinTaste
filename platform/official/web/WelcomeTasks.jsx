import {t} from './i18n.mjs';
import React,{useState} from 'react';
import {Search,BookOpen,Workflow,BarChart3,RefreshCw} from 'lucide-react';

const groups=[
 [
  {name:'政策检索',icon:Search,tone:'blue',text:'梳理固废资源化政策与适用条件',prompt:'帮我梳理固体废物资源化利用的现行政策和适用条件，列出依据、适用对象及需要进一步核实的事项。'},
  {name:'文献研读',icon:BookOpen,tone:'violet',text:'提炼论文中的方法、结果与局限',prompt:'帮我阅读上传的文献，梳理研究问题、关键方法、主要结果和局限，并标明原文依据。'},
  {name:'技术比较',icon:Workflow,tone:'amber',text:'比较不同资源化工艺的适用场景',prompt:'帮我比较不同固废资源化工艺的适用场景、关键参数和选择依据，资料不足的部分请明确标出。'},
  {name:'数据解读',icon:BarChart3,tone:'mint',text:'从材料中核对数据、梳理结论',prompt:'帮我核对上传材料中的关键数据、单位和来源，梳理可以支持的结论，并指出证据不足或存在矛盾的地方。'},
 ],
 [
  {name:'监管核查',icon:Search,tone:'blue',text:'整理现场核查的重点与资料清单',prompt:'帮我整理固废现场核查的重点、需要准备的资料和待核实问题，给出对应依据。'},
  {name:'科研思路',icon:BookOpen,tone:'violet',text:'梳理研究问题与证据缺口',prompt:'基于我提供的研究材料，帮我梳理核心研究问题、已有证据和仍需补充验证的环节。'},
  {name:'工艺分析',icon:Workflow,tone:'amber',text:'梳理工艺流程及关键控制环节',prompt:'基于上传材料梳理工艺流程、关键控制环节和参数关系，不确定的信息请注明。'},
  {name:'结论核对',icon:BarChart3,tone:'mint',text:'检查结论与原始数据是否一致',prompt:'帮我检查上传材料中的结论是否得到原始数据支持，逐项列出数据、结论及需要修正或补充的依据。'},
 ],
];

export default function WelcomeTasks({onPick,disabled=false}){
 const [group,setGroup]=useState(0);
 return <section className="welcome-tasks" aria-label={t("推荐问题")}>
  <div className="welcome-task-grid">{groups[group].map(task=>{const Icon=task.icon;return <button type="button" key={task.name} className="welcome-task" disabled={disabled} onClick={()=>onPick(t(task.prompt))} aria-label={t("填入问题：{v0}", {v0: t(task.text)})}>
   <span className={`welcome-task-logo ${task.tone}`} aria-hidden="true"><Icon size={24} strokeWidth={1.8}/></span>
   <strong>{t(task.name)}</strong><span className="welcome-task-copy">{t(task.text)}</span>
  </button>;})}</div>
  <button type="button" className="welcome-task-refresh" disabled={disabled} onClick={()=>setGroup(current=>(current+1)%groups.length)}><RefreshCw size={15}/>{t("换一换")}</button>
 </section>;
}
