import { tool } from "@opencode-ai/plugin"
const base=()=>process.env.RIPPLE_TOOL_BASE||"";const token=()=>process.env.RIPPLE_AGENT_TOOL_TOKEN||"";
async function post(body:unknown){const r=await fetch(base()+"/api/ripple-agent/tools/campaign/fetch",{method:"POST",headers:{"Content-Type":"application/json","X-Ripple-Agent-Token":token()},body:JSON.stringify(body)});const t=await r.text();if(!r.ok)throw new Error(`Ripple tool failed (${r.status})`);return t}
export default tool({description:"只读获取 Ripple 已验证的 B站活动页面证据。仅支持 bilibili.com 活动 URL；不会开放任意 Web Fetch、登录或写操作。",args:{campaign_id:tool.schema.string().optional(),url:tool.schema.string().optional()},async execute(args){return post({campaign_id:args.campaign_id||"",url:args.url||""})}})
