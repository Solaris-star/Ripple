import { tool } from "@opencode-ai/plugin"
const base=()=>process.env.RIPPLE_TOOL_BASE||"";const token=()=>process.env.RIPPLE_AGENT_TOOL_TOKEN||"";
async function post(body:unknown){const r=await fetch(base()+"/api/ripple-agent/tools/publish/status",{method:"POST",headers:{"Content-Type":"application/json","X-Ripple-Agent-Token":token()},body:JSON.stringify(body)});const t=await r.text();if(!r.ok)throw new Error(`Ripple tool failed (${r.status})`);return t}
export default tool({description:"读取 Ripple 发布任务状态、审核状态与已有回执。只读，不审核、不执行发布。",args:{task_id:tool.schema.string().max(64).optional(),limit:tool.schema.number().int().min(1).max(50).optional()},async execute(args){return post({task_id:args.task_id||"",limit:args.limit||20})}})
