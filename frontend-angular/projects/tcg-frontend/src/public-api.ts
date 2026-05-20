/// <reference path="./typings.d.ts" />
/*
 * Public API Surface of @tcg/frontend
 *
 * Everything a host application can import from the library MUST be
 * re-exported here. Anything not listed below is considered internal
 * and may change without notice between waves.
 *
 * G4: every public symbol is prefixed `tcg-` / `Tcg` / `TCG_`. No
 * back-compat exception — unprefixed aliases are not part of the
 * public API.
 */

// Core / wave 0 ---------------------------------------------------------
export { TcgApiService } from './lib/api/tcg-api.service';
export { TCG_API_BASE_URL } from './lib/api/tcg-api.tokens';
export { TcgSmokeComponent } from './lib/tcg-smoke.component';

// API services ---------------------------------------------------------
export { TcgDataApi } from './lib/api/tcg-data-api.service';
export type {
  TcgInstrumentItem,
  TcgInstrumentList,
  TcgPriceSeries,
  TcgContinuousOpts,
  TcgInstrumentPricesOpts,
} from './lib/api/tcg-data-api.service';

// `describePersistenceError` (unprefixed helper) is intentionally NOT
// re-exported — library-internal, consumed by TcgPersistenceApi and its
// spec via relative import.
export { TcgPersistenceApi, TCG_PERSISTENCE_CATEGORIES } from './lib/api/tcg-persistence-api.service';
export type {
  TcgPersistenceCategory,
  TcgSignalOut,
  TcgSignalCreatePayload,
  TcgPortfolioOut,
  TcgPortfolioCreatePayload,
  TcgBasketOut,
  TcgBasketCreatePayload,
} from './lib/api/tcg-persistence-api.service';

// Services -------------------------------------------------------------
export {
  TcgUserSettingsService,
  TCG_LS_KEYS,
  TCG_DEFAULT_RISK_FREE_RATE_PCT,
  TCG_DEFAULT_RISK_FREE_RATE_FRACTION,
} from './lib/services/tcg-user-settings.service';
export type { TcgTheme, TcgChartType } from './lib/services/tcg-user-settings.service';

export {
  TcgBackendAutosaveService,
  TCG_DEFAULT_AUTOSAVE_DEBOUNCE_MS,
} from './lib/services/tcg-backend-autosave.service';
export type {
  TcgSaveStatus,
  TcgBackendAutosaveRegistration,
  TcgBackendAutosaveHandle,
} from './lib/services/tcg-backend-autosave.service';

export { TcgAutosaveService } from './lib/services/tcg-autosave.service';
export type { TcgAutosaveRegistration } from './lib/services/tcg-autosave.service';

export { TcgAbortableActionService } from './lib/services/tcg-abortable-action.service';

// Chart subsystem ------------------------------------------------------
// Chart implementation helpers (buildBaseLayout, getChartColors,
// CHART_CONFIG, TRACE_COLORS, createVerticalLineTrace, hiddenOverlayAxis,
// buildAllMarkerTraces, buildMarkerTrace, buildMarkerHovertemplate,
// buildCsv, downloadCsv) are deliberately NOT re-exported. They are
// library-internal — consumed by TcgChartComponent via relative imports.
export { TcgChartComponent } from './lib/components/chart/tcg-chart.component';
export { TcgPlotlyService } from './lib/components/chart/tcg-plotly.service';
export type { TcgPlotlyModule } from './lib/components/chart/tcg-plotly.service';
export type { TcgChartPalette } from './lib/components/chart/chart-theme';
export type { TcgChartMarker, TcgContractMeta } from './lib/components/chart/chart-markers';
export type { TcgCsvTrace } from './lib/components/chart/chart-csv';

// Components -----------------------------------------------------------
export { TcgIconComponent } from './lib/components/icon/tcg-icon.component';
export { TcgCardComponent } from './lib/components/card/tcg-card.component';
export { TcgInlineNameInputComponent } from './lib/components/inline-name-input/tcg-inline-name-input.component';
export type { TcgRenamableEntity } from './lib/components/inline-name-input/tcg-inline-name-input.component';
export { TcgPillToggleComponent } from './lib/components/pill-toggle/tcg-pill-toggle.component';
export type { TcgPillOption } from './lib/components/pill-toggle/tcg-pill-toggle.component';
export { TcgPlaceholderPageComponent } from './lib/components/placeholder-page/tcg-placeholder-page.component';
export { TcgPlaceholderRouteComponent } from './lib/components/placeholder-page/tcg-placeholder-route.component';
export { TcgRfrInputComponent } from './lib/components/risk-free-rate-input/tcg-rfr-input.component';
export { TcgSaveStatusComponent } from './lib/components/save-status/tcg-save-status.component';
export type { TcgSaveStatusValue } from './lib/components/save-status/tcg-save-status.component';
export { TcgSaveControlsComponent } from './lib/components/save-controls/tcg-save-controls.component';
export { TcgConfirmDialogComponent } from './lib/components/confirm-dialog/tcg-confirm-dialog.component';
export {
  TcgErrorBoundaryComponent,
  TcgErrorBoundaryHandler,
} from './lib/components/error-boundary/tcg-error-boundary.component';
export { TcgErrorCardComponent } from './lib/components/error-card/tcg-error-card.component';
export type { TcgErrorEnvelope } from './lib/components/error-card/tcg-error-card.component';

// InstrumentPickerModal subsystem (8 components) -----------------------
export { TcgInstrumentPickerModalComponent } from './lib/components/instrument-picker-modal/tcg-instrument-picker-modal.component';
export { TcgBasketComposerComponent } from './lib/components/instrument-picker-modal/tcg-basket-composer.component';
export { TcgBasketLegRowComponent } from './lib/components/instrument-picker-modal/tcg-basket-leg-row.component';
export { TcgContinuousSpecPickerComponent } from './lib/components/instrument-picker-modal/tcg-continuous-spec-picker.component';
export { TcgContinuousLegPickerComponent } from './lib/components/instrument-picker-modal/tcg-continuous-leg-picker.component';
export { TcgOptionStreamPickerComponent } from './lib/components/instrument-picker-modal/tcg-option-stream-picker.component';
export { TcgOptionLegPickerComponent } from './lib/components/instrument-picker-modal/tcg-option-leg-picker.component';
export { TcgSpotLegPickerComponent } from './lib/components/instrument-picker-modal/tcg-spot-leg-picker.component';
export type { TcgSpotCandidate } from './lib/components/instrument-picker-modal/tcg-spot-leg-picker.component';
export {
  tcgInstrumentTypeForAssetClass,
  tcgCollectionsForAssetClass,
} from './lib/components/instrument-picker-modal/types';
export type {
  TcgSpotInstrumentRef,
  TcgContinuousInstrumentRef,
  TcgOptionStreamRef,
  TcgSavedBasketRef,
  TcgInlineBasketRef,
  TcgInstrumentLeg,
  TcgInstrumentDescriptor,
  TcgBasketAssetClass,
} from './lib/components/instrument-picker-modal/types';

// Layout ---------------------------------------------------------------
export { TcgSidebarComponent } from './lib/layout/tcg-sidebar.component';
export { TcgPageContainerComponent } from './lib/layout/tcg-page-container.component';
export { TCG_NAV_SECTIONS } from './lib/layout/nav-config';
export type { TcgNavItem, TcgNavSection } from './lib/layout/nav-config';

// Routes ---------------------------------------------------------------
export { tcgRoutes } from './lib/tcg-routes';
