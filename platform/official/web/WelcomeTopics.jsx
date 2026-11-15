import {t} from './i18n.mjs';
import React from 'react';
import {Landmark,Building2,FlaskConical,ArrowUpRight,Lightbulb} from 'lucide-react';

const topics=[
 {id:'government',Icon:Landmark,label:'政府监管',description:'梳理核查步骤，明确资料与证据',questions:[
  {title:'生成现场核查清单',detail:'疑似堆场 · 线索与证据',text:'请为疑似固废堆场制定现场核查清单，区分可见线索、待核实事项与证据要求，并列出需要补充的现场资料。'},
  {title:'梳理台账与资料核查',detail:'来源去向 · 贮存与处置记录',text:'请为固体废物产生单位整理资料核查清单，涵盖来源、分类、数量、贮存、转移和处置记录，并说明各项应核对的信息。'}
 ]},
 {id:'enterprise',Icon:Building2,label:'企业管理',description:'从生产实际出发，完善管理方案',questions:[
  {title:'制定分类与台账方案',detail:'生产环节 · 责任分工',text:'请提供制造企业固废分类和台账管理的初步方案，说明需要补充的资料，并梳理生产环节、责任分工和日常记录要求。'},
  {title:'比较资源化工艺路线',detail:'物料特性 · 工艺与验证',text:'我想比较一种固废的资源化利用路线。请先列出需要了解的物料特性、产量和现有工艺，再提供工艺比较维度和验证步骤。'}
 ]},
 {id:'research',Icon:FlaskConical,label:'科研分析',description:'厘清研究问题，设计验证路径',questions:[
  {title:'设计模型评估方案',detail:'空间泛化 · 误报与漏报',text:'请设计固废遥感识别模型的评估方案，考虑空间泛化、误报漏报和不确定性，并说明数据划分、评价指标和验证步骤。'},
  {title:'梳理文献与研究空白',detail:'检索思路 · 假设与实验',text:'请围绕固废遥感识别整理文献检索思路，比较现有技术路线、常见评估方法和潜在研究空白，并指出需要验证的假设与实验。'}
 ]}
];

export default function WelcomeTopics({onChoose,disabled}){
 return <section className="welcome-topics" aria-labelledby="welcome-topics-title">
  <div className="welcome-topics-heading"><h2 id="welcome-topics-title">{t("从一个具体问题开始")}</h2><p>{t("选择下面的问题，编辑后发送")}</p></div>
  <div className="welcome-topics-grid">{topics.map(({id,Icon,label,description,questions})=><section className="welcome-topic-card" key={id} aria-labelledby={`welcome-topic-${id}`}>
   <header><span className="welcome-topic-icon" aria-hidden="true"><Icon size={22}/></span><div><h3 id={`welcome-topic-${id}`}>{t(label)}</h3><p>{t(description)}</p></div></header>
   <div className="welcome-topic-questions">{questions.map(question=><button type="button" key={question.title} disabled={disabled} onClick={()=>onChoose(t(question.text))} aria-label={t(question.title)}><span><strong>{t(question.title)}</strong><small>{t(question.detail)}</small></span><ArrowUpRight size={17} aria-hidden="true"/></button>)}</div>
  </section>)}</div>
  <p className="welcome-topics-tip"><Lightbulb size={16} aria-hidden="true"/><span>{t("补充对象、背景与目标，也可以附上资料，让问题更具体。")}</span></p>
 </section>;
}
