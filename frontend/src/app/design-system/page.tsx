import {
  Bell,
  Calendar,
  Check,
  Flame,
  Plus,
  RefreshCw,
  Trash2,
  TrendingUp,
} from 'lucide-react';
import { Badge, Button, Card, PageHeader } from '@/components/ui';

/**
 * Living style guide for the design system. Public route (no auth, no backend)
 * so the system can be reviewed in isolation. Visit /design-system.
 */
export default function DesignSystemPage() {
  return (
    <main className="mx-auto max-w-4xl px-6 py-10">
      <PageHeader
        eyebrow="Design system"
        title="Jess UI kit"
        description="Semantic tokens + shared primitives. Everything below is driven by tailwind.config.ts + globals.css."
        icon={<TrendingUp size={24} />}
        actions={<Button size="sm" variant="secondary"><RefreshCw size={14} /> Refresh</Button>}
      />

      {/* Surfaces ----------------------------------------------------------- */}
      <Section title="Surfaces" eyebrow="Elevation">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Swatch label="surface-base" className="bg-surface-base" />
          <Swatch label="surface-raised" className="bg-surface-raised" />
          <Swatch label="surface-overlay" className="bg-surface-overlay" />
          <Swatch label="surface-inset" className="bg-surface-inset" />
        </div>
      </Section>

      {/* Text + status colors ----------------------------------------------- */}
      <Section title="Content & status" eyebrow="Color intent">
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          <span className="text-content-primary">content-primary</span>
          <span className="text-content-secondary">content-secondary</span>
          <span className="text-content-muted">content-muted</span>
          <span className="text-brand">brand</span>
          <span className="text-success">success</span>
          <span className="text-warning">warning</span>
          <span className="text-danger">danger</span>
          <span className="text-info">info</span>
        </div>
      </Section>

      {/* Typography --------------------------------------------------------- */}
      <Section title="Typography" eyebrow="Scale">
        <div className="space-y-2">
          <p className="text-display">Display — page titles</p>
          <p className="text-title">Title — card headers</p>
          <p className="text-eyebrow">Eyebrow — section kicker</p>
          <p className="text-label">Label — field labels</p>
          <p className="text-caption">Caption — hints & metadata</p>
        </div>
      </Section>

      {/* Buttons ------------------------------------------------------------ */}
      <Section title="Buttons" eyebrow="Actions">
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="primary"><Plus size={16} /> Primary</Button>
          <Button variant="secondary">Secondary</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="danger"><Trash2 size={16} /> Danger</Button>
          <Button variant="primary" disabled>Disabled</Button>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <Button size="sm" variant="primary">Small</Button>
          <Button size="sm" variant="secondary">Small</Button>
          <Button size="sm" variant="ghost"><RefreshCw size={14} /> Small ghost</Button>
        </div>
      </Section>

      {/* Badges ------------------------------------------------------------- */}
      <Section title="Badges" eyebrow="Status pills">
        <div className="flex flex-wrap gap-2">
          <Badge tone="neutral">neutral</Badge>
          <Badge tone="brand">brand</Badge>
          <Badge tone="success"><Check size={12} /> success</Badge>
          <Badge tone="warning">warning</Badge>
          <Badge tone="danger">danger</Badge>
          <Badge tone="info">info</Badge>
        </div>
      </Section>

      {/* Inputs ------------------------------------------------------------- */}
      <Section title="Inputs" eyebrow="Forms">
        <div className="max-w-sm space-y-3">
          <input className="input" placeholder="Search documents…" />
          <textarea className="input" rows={2} placeholder="Brain dump…" />
        </div>
      </Section>

      {/* Cards in context --------------------------------------------------- */}
      <Section title="Cards in context" eyebrow="Composition">
        <div className="grid gap-4 sm:grid-cols-2">
          {/* A dashboard-style card */}
          <Card>
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-title flex items-center gap-2">
                <Calendar size={18} className="text-brand" /> Today
              </h3>
              <Badge tone="info">3 events</Badge>
            </div>
            <div className="card-inset space-y-1 p-3">
              <p className="text-sm text-content-primary">Standup — 9:00</p>
              <p className="text-caption">Zoom · 30 min</p>
            </div>
          </Card>

          {/* A gamified finance-style card — same tokens, distinct character */}
          <Card>
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-title flex items-center gap-2">
                <Flame size={18} className="text-warning" /> Streak
              </h3>
              <Badge tone="warning">7 days</Badge>
            </div>
            <div className="mb-2 flex items-end justify-between">
              <span className="text-eyebrow">Level 4 · Saver</span>
              <span className="font-mono text-success">+120 XP</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-surface-inset">
              <div className="h-full w-2/3 rounded-full bg-brand" />
            </div>
          </Card>
        </div>
      </Section>

      {/* Notification rows (the "announcement color explosion" — now tokens) */}
      <Section title="Status rows" eyebrow="Was 15 ad-hoc colors">
        <div className="space-y-2">
          {[
            { tone: 'info' as const, icon: Calendar, text: 'Calendar synced' },
            { tone: 'success' as const, icon: Check, text: 'Meds logged' },
            { tone: 'warning' as const, icon: Bell, text: 'Reminder snoozed' },
            { tone: 'danger' as const, icon: Bell, text: 'Token refresh failed' },
          ].map(({ tone, icon: Icon, text }, i) => (
            <div key={i} className="card-inset flex items-center gap-3 p-3">
              <span className={`badge badge-${tone} !rounded-md p-1.5`}>
                <Icon size={14} />
              </span>
              <span className="text-sm text-content-primary">{text}</span>
            </div>
          ))}
        </div>
      </Section>
    </main>
  );
}

function Section({
  title,
  eyebrow,
  children,
}: {
  title: string;
  eyebrow: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-10">
      <p className="text-eyebrow mb-1">{eyebrow}</p>
      <h2 className="text-title mb-4 border-b border-line-subtle pb-2">{title}</h2>
      {children}
    </section>
  );
}

function Swatch({ label, className }: { label: string; className: string }) {
  return (
    <div>
      <div className={`h-14 rounded-lg border border-line ${className}`} />
      <p className="text-caption mt-1.5 font-mono">{label}</p>
    </div>
  );
}
