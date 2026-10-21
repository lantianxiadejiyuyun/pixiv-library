export interface PerformancePoint {at:number;host_cpu:number|null;service_cpu:number|null;host_memory:number|null;service_memory:number|null;download_bps:number|null;upload_bps:number|null}
export interface ProcessMetric {lane:string;pid:number|null;state:string;cpu_percent:number|null;rss_bytes:number|null;cpus:number[];restarts:number}
export interface PerformanceSnapshot {
  status:'ready'|'sampling'|'stale';sample_age_seconds:number|null;refresh_seconds:number;
  current:{sampled_at:string;platform:string;container:boolean;uptime_seconds:number;service_uptime_seconds:number;worker_mode:string;
    cpu:{host_percent:number|null;service_percent:number|null;logical_cpus:number;service_capacity:number;load_average:number[]|null};
    memory:{host:{total_bytes:number;used_bytes:number;available_bytes:number;percent:number};service:{used_bytes:number|null;current_bytes:number|null;limit_bytes:number;percent:number|null;basis:string;oom_kills:number|null}};
    network:{download_bps:number|null;upload_bps:number|null;scope:string};processes:ProcessMetric[]}|null;
  history:PerformancePoint[];
  database:{status:string;latency_ms:number|null;stale:boolean;pool:{size:number;checked_out:number;overflow:number}|null;queues:{id:string;label:string;paused?:boolean;effective_paused?:boolean;concurrency?:number;queued:number;running:number}[]}|null;
  storage:{id:string;label:string;status:string;total_bytes?:number;used_bytes?:number;free_bytes?:number;percent?:number}[];storage_stale:boolean;
}
export function percent(value:number|null|undefined){return value==null||!Number.isFinite(value)?'采样中':`${value.toFixed(1)}%`}
export function uptime(value:number){const d=Math.floor(value/86400),h=Math.floor(value%86400/3600),m=Math.floor(value%3600/60);return `${d?`${d}天 `:''}${h}小时 ${m}分`}
export function chartLines(points:PerformancePoint[],key:keyof PerformancePoint,maximum=100):string[]{
  if(!points.length)return []
  const end=points[points.length-1]!.at,result:string[]=[];let line:string[]=[]
  for(const point of points){const value=point[key];if(value==null||!Number.isFinite(value)){if(line.length)result.push(line.join(' '));line=[];continue}
    line.push(`${Math.max(0,Math.min(600,(point.at-end+300)/300*600)).toFixed(2)},${(138-Math.max(0,Math.min(1,value/maximum))*130).toFixed(2)}`)
  }if(line.length)result.push(line.join(' '));return result
}
