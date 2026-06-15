import FeatureGate from '@/components/layout/FeatureGate';

export default function MealsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <FeatureGate flag="meals_enabled" label="Meals">
      {children}
    </FeatureGate>
  );
}
