// Wizard progress indicator — numbered, labelled steps with connectors.
// Labels are hidden on small screens (circles + connectors only) so the row
// stays within the wizard card as more steps are added.

interface StepperProps {
  steps: string[];
  current: number;
}

export default function Stepper({ steps, current }: StepperProps) {
  return (
    <ol className="flex items-center" aria-label="Setup progress">
      {steps.map((label, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <li
            key={label}
            className="flex flex-1 items-center gap-2 last:flex-none"
            aria-current={active ? 'step' : undefined}
          >
            <span
              className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
                active
                  ? 'bg-brand-600 text-white'
                  : done
                    ? 'bg-brand-600/25 text-brand-500'
                    : 'bg-zinc-800 text-zinc-500'
              }`}
            >
              {done ? '✓' : i + 1}
            </span>
            <span
              className={`hidden whitespace-nowrap text-sm sm:inline ${
                active ? 'text-white' : 'text-zinc-500'
              }`}
            >
              {label}
              {(active || done) && (
                <span className="sr-only">
                  {active ? ' (current step)' : ' (completed)'}
                </span>
              )}
            </span>
            {i < steps.length - 1 && (
              <span
                className={`mx-1 h-px flex-1 ${done ? 'bg-brand-600/40' : 'bg-zinc-800'}`}
              />
            )}
          </li>
        );
      })}
    </ol>
  );
}
