import FeatureGate from '@/components/layout/FeatureGate';

export default function WorkoutsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <FeatureGate flag="workouts_enabled" label="Workouts">
      {children}
    </FeatureGate>
  );
}
