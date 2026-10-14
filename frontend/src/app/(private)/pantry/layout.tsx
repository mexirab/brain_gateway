import FeatureGate from '@/components/layout/FeatureGate';

export default function PantryLayout({ children }: { children: React.ReactNode }) {
  return (
    <FeatureGate flag="pantry_enabled" label="Pantry">
      {children}
    </FeatureGate>
  );
}
