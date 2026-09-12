import { tool } from "@opencode-ai/plugin"
const base=()=>process.env.RIPPLE_TOOL_BASE||""; const token=()=>process.env.RIPPLE_AGENT_TOOL_TOKEN||"";
async function post(path:string,body:unknown){const r=await fetch(base()+path,{method:"POST",headers:{"Content-Type":"application/json","X-Ripple-Agent-Token":token()},body:JSON.stringify(body)});const t=await r.text();if(!r.ok)throw new Error(`Ripple tool failed (${r.status})`);return t}
export const list=tool({description:"读取 Ripple 选题库。只读。",args:{limit:tool.schema.number().int().min(1).max(50).optional()},async execute(args){return post("/api/ripple-agent/tools/ideas/list",{limit:args.limit||20})}})
export const add=tool({description:"把候选加入 Ripple 选题库。不会创建内容或发布任务。",args:{title:tool.schema.string().min(1).max(160),note:tool.schema.string().max(1200).optional()},async execute(args){return post("/api/ripple-agent/tools/ideas/add",{title:args.title,note:args.note||""})}})
