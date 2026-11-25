import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import {
  ArrowLeftRight,
  BarChart3,
  Boxes,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Coins,
  FlaskConical,
  Gauge,
  LayoutDashboard,
  Menu,
  Play,
  ScrollText,
  Share2,
  Target,
  Workflow,
  Wrench,
  ShieldAlert,
} from 'lucide-react'

interface NavItem {
  to: string
  label: string
  icon: typeof LayoutDashboard
}

interface NavGroup {
  id: string
  title: string
  icon: typeof LayoutDashboard
  items: NavItem[]
}

const GROUPS: NavGroup[] = [
  {
    id: 'workspace',
    title: '工作台',
    icon: LayoutDashboard,
    items: [
      { to: '/', label: '总览', icon: LayoutDashboard },
    ],
  },
  {
    id: 'agent',
    title: 'Agent 执行与轨迹',
    icon: Play,
    items: [
      { to: '/runs', label: 'Agent 运行', icon: Play },
      { to: '/traces', label: '轨迹浏览', icon: ScrollText },
      { to: '/dag', label: 'DAG 分析', icon: Share2 },
    ],
  },
  {
    id: 'diagnostics',
    title: '故障诊断',
    icon: Target,
    items: [
      { to: '/fault-lab', label: '故障实验室', icon: FlaskConical },
      { to: '/attribution', label: '故障归因', icon: Target },
      { to: '/counterfactual', label: '反事实分析', icon: Workflow },
      { to: '/repair', label: '修复工作台', icon: Wrench },
      { to: '/comparison', label: '轨迹对比', icon: ArrowLeftRight },
    ],
  },
  {
    id: 'evaluation',
    title: '评测中心',
    icon: BarChart3,
    items: [
      { to: '/evaluation', label: '评测总览', icon: BarChart3 },
      { to: '/pipelines', label: '评测流水线', icon: Workflow },
      { to: '/version-compare', label: '版本对比', icon: ArrowLeftRight },
      { to: '/calibration', label: 'Judge 校准', icon: Gauge },
      { to: '/multi-judge', label: '多 Judge 分歧', icon: ArrowLeftRight },
    ],
  },
  {
    id: 'security',
    title: '安全与性能',
    icon: ShieldAlert,
    items: [
      { to: '/redteam', label: '红队测试', icon: ShieldAlert },
      { to: '/adversarial', label: '对抗评测', icon: FlaskConical },
      { to: '/costs', label: '成本分析', icon: Coins },
      { to: '/pareto', label: 'Pareto 分析', icon: Gauge },
    ],
  },
  {
    id: 'system',
    title: '系统',
    icon: Boxes,
    items: [
      { to: '/architecture', label: '系统架构', icon: Boxes },
    ],
  },
]

function findActiveGroup(pathname: string): string {
  return (
    GROUPS.find((group) =>
      group.items.some((item) => item.to === pathname)
    )?.id ?? 'workspace'
  )
}

export default function Layout() {
  const location = useLocation()

  const [collapsed, setCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)

  const [expandedGroups, setExpandedGroups] = useState<string[]>([
    findActiveGroup(location.pathname),
  ])

  useEffect(() => {
    const active = findActiveGroup(location.pathname)

    setExpandedGroups((previous) =>
      previous.includes(active)
        ? previous
        : [...previous, active]
    )
  }, [location.pathname])

  function toggleGroup(id: string) {
    setExpandedGroups((previous) =>
      previous.includes(id)
        ? previous.filter((value) => value !== id)
        : [...previous, id]
    )
  }

  return (
    <div className="app">
      <aside
        className={
          'sidebar' +
          (collapsed ? ' collapsed' : '') +
          (mobileOpen ? ' open' : '')
        }
      >
        <div className="brand">
          <div className="logo">TP</div>
          <div className="name">TracePilot</div>
        </div>

        <nav className="nav" aria-label="主导航">
          {GROUPS.map((group) => {
            const expanded = expandedGroups.includes(group.id)
            const active = group.items.some(
              (item) => item.to === location.pathname
            )

            return (
              <div className="nav-group" key={group.id}>
                <button
                  type="button"
                  className={
                    'nav-group-trigger' +
                    (active ? ' group-active' : '')
                  }
                  onClick={() => {
                    if (collapsed) {
                      setCollapsed(false)
                      setExpandedGroups((previous) =>
                        previous.includes(group.id)
                          ? previous
                          : [...previous, group.id]
                      )
                    } else {
                      toggleGroup(group.id)
                    }
                  }}
                  aria-expanded={expanded && !collapsed}
                  title={group.title}
                >
                  <group.icon size={18} aria-hidden />

                  <span className="nav-label">
                    {group.title}
                  </span>

                  {!collapsed && (
                    <ChevronDown
                      size={15}
                      className={
                        'group-chevron' +
                        (expanded ? ' expanded' : '')
                      }
                      aria-hidden
                    />
                  )}
                </button>

                {expanded && !collapsed && (
                  <div className="nav-group-items">
                    {group.items.map((item) => (
                      <NavLink
                        key={item.to}
                        to={item.to}
                        end={item.to === '/'}
                        className={({ isActive }) =>
                          'nav-item nav-subitem' +
                          (isActive ? ' active' : '')
                        }
                        onClick={() => setMobileOpen(false)}
                      >
                        <item.icon size={16} aria-hidden />

                        <span className="nav-label">
                          {item.label}
                        </span>
                      </NavLink>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </nav>

        <div className="foot">
          <button
            className="collapse-btn"
            onClick={() => setCollapsed((v) => !v)}
            aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
            aria-expanded={!collapsed}
            title={collapsed ? '展开侧栏' : '收起侧栏'}
          >
            {collapsed ? (
              <ChevronRight size={17} />
            ) : (
              <ChevronLeft size={17} />
            )}

            <span className="nav-label">
              {collapsed ? '展开侧栏' : '收起侧栏'}
            </span>
          </button>
        </div>
      </aside>

      <div className="main">
        <div className="topbar">
          <button
            className="btn mobile-toggle"
            onClick={() => setMobileOpen((v) => !v)}
            aria-label="菜单"
          >
            <Menu size={16} />
          </button>

          <h1>TracePilot</h1>

          <div className="spacer" />
        </div>

        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}