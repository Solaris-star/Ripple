import React from 'react';
import { createRoot } from 'react-dom/client';
import InteractionCenter from '../src/components/workspace/InteractionCenter';
import ContentVariantPublisher from '../src/components/workspace/ContentVariantPublisher';
import '../src/index.css';

const seed = await fetch('/__fixture/state').then(response => response.json());
createRoot(document.getElementById('root')!).render(seed.component === 'variant'
  ? <ContentVariantPublisher variantId={seed.variantId} source={seed.mother} onNavigate={() => {}} onUpdated={() => {}} />
  : <InteractionCenter persona="" onNavigate={() => {}} />);
