import type { Page } from './Sidebar';
import type { ComponentType } from 'react';
import { IconDashboard, IconFire, IconIdea, IconCalendar, IconPublish, IconSkills, IconChart } from './icons';

interface SubNavProps {
  current: Page;
  onNavigate: (page: Page) => void;
}

type ToolItem = { page: Page; Icon: ComponentType<{ size?: number }>; label: string };

const TOPIC_TOOLS: ToolItem[] = [
  { page: 'trends', Icon: IconFire, label: '热点雷达' },
  { page: 'ideas', Icon: IconIdea, label: '选题库' },
  { page: 'planning', Icon: IconCalendar, label: '选题日历' },
  { page: 'breakdown', Icon: IconSkills, label: '爆款拆解' },
];

const PUBLISH_TOOLS: ToolItem[] = [
  { page: 'publish', Icon: IconPublish, label: '发布任务' },
  { page: 'interactions', Icon: IconSkills, label: '互动管理' },
  { page: 'calendar', Icon: IconCalendar, label: '发布日历' },
  { page: 'analytics', Icon: IconChart, label: '发布记录' },
];

export default function SubNav({ current, onNavigate }: SubNavProps) {
  const publishGroup = PUBLISH_TOOLS.some((item) => item.page === current);
  const tools = publishGroup ? PUBLISH_TOOLS : TOPIC_TOOLS;

  return (
    <div className="subnav">
      <button className="subnav-back" onClick={() => onNavigate('dashboard')} title="返回首页">
        <IconDashboard size={15} /> 首页
      </button>
      <span className="subnav-div" />
      <div className="subnav-tabs">
        {tools.map(({ page, Icon, label }) => (
          <button key={page} className={`subnav-tab ${current === page ? 'active' : ''}`}
            onClick={() => onNavigate(page)}>
            <Icon size={14} />{label}
          </button>
        ))}
      </div>
    </div>
  );
}
