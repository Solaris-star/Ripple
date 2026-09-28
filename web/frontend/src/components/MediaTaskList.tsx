import { useEffect, useState } from 'react';
import { mediaUrl } from '../lib/api';
import { finishMediaTasks, mediaElapsed, type MediaTask } from '../lib/mediaTask';
import '../styles/media-task.css';

interface Props {
  tasks: MediaTask[];
  live?: boolean;
  onRetry?: () => void;
  onUpload?: () => void;
}

export default function MediaTaskList({ tasks, live, onRetry, onUpload }: Props) {
  const [now, setNow] = useState(Date.now);
  const running = live && tasks.some((task) => task.status === 'running' || task.status === 'pending');
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  const visible = live ? tasks : finishMediaTasks(tasks, now);
  return <div className="r2-media-tasks" aria-label="图片生成状态">
    {visible.map((task) => {
      const active = task.status === 'pending' || task.status === 'running';
      const failed = task.status === 'failed' || task.status === 'interrupted';
      const label = task.status === 'completed' ? '图片已生成' : task.status === 'pending' ? '图片生成等待中'
        : task.status === 'running' ? '正在生成图片' : task.status === 'failed' ? '图片生成失败' : '图片生成未完成';
      return <section key={task.id} className={`r2-media-task ${failed ? 'failed' : active ? 'running' : 'completed'}`}>
        <div className="r2-media-task-heading" role="status"><strong>{active && <span className="live-pulse" />}{label}</strong><small>{active ? '已等待' : '耗时'} {mediaElapsed(task, now)}</small></div>
        {active && <p>正在等待图片工具返回结果。可以继续编辑主稿，或上传已有图片。</p>}
        {failed && <p role="alert">{task.error || '没有收到可用的图片产物。'}</p>}
        {task.status === 'completed' && task.path && <><a href={mediaUrl(task.path)} target="_blank" rel="noreferrer"><img src={mediaUrl(task.path)} alt="生成的配图" loading="lazy" /></a><p>已保存到素材与成品</p></>}
        {(failed || active) && <div className="r2-media-task-actions">{failed && onRetry && <button onClick={onRetry}>重试图片步骤</button>}{onUpload && <button onClick={onUpload}>上传已有图片</button>}</div>}
      </section>;
    })}
  </div>;
}
