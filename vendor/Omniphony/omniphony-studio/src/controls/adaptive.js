/**
 * Adaptive resampling controls.
 *
 * Extracted from app.js (lines 3899-4040).
 */

import { app, dirty, hasProducerDomain } from '../state.js';
import { formatNumber } from '../coordinates.js';
import { scheduleUIFlush } from '../flush.js';
import { t } from '../i18n.js';
import { inAudioPanel } from '../ui/panel-roots.js';

function getAdaptiveResamplingToggleEl() { return inAudioPanel('adaptiveResamplingToggle'); }
function getAdaptiveFarHardRecoverHighToggleEl() { return inAudioPanel('adaptiveFarHardRecoverHighToggle'); }
function getAdaptiveFarHardRecoverLowToggleEl() { return inAudioPanel('adaptiveFarHardRecoverLowToggle'); }
function getAdaptiveFarSilenceToggleEl() { return inAudioPanel('adaptiveFarSilenceToggle'); }
function getAdaptiveFarSilenceRowEl() { return inAudioPanel('adaptiveFarSilenceRow'); }
function getAdaptiveFarFadeRowEl() { return inAudioPanel('adaptiveFarFadeRow'); }
function getAdaptiveFarFadeInMsInputEl() { return inAudioPanel('adaptiveFarFadeInMsInput'); }
function getAdaptiveUpdateIntervalRowEl() { return inAudioPanel('adaptiveUpdateIntervalRow'); }
function getAdaptiveKpNearInputEl() { return inAudioPanel('adaptiveKpNearInput'); }
function getAdaptiveKpNearRowEl() { return inAudioPanel('adaptiveKpNearRow'); }
function getAdaptiveKiInputEl() { return inAudioPanel('adaptiveKiInput'); }
function getAdaptiveKiRowEl() { return inAudioPanel('adaptiveKiRow'); }
function getAdaptiveIntegralDischargeRatioInputEl() { return inAudioPanel('adaptiveIntegralDischargeRatioInput'); }
function getAdaptiveIntegralDischargeRowEl() { return inAudioPanel('adaptiveIntegralDischargeRow'); }
function getAdaptiveMaxAdjustInputEl() { return inAudioPanel('adaptiveMaxAdjustInput'); }
function getAdaptiveMaxAdjustRowEl() { return inAudioPanel('adaptiveMaxAdjustRow'); }
function getAdaptiveHighRecoverEntryMarginRowEl() { return inAudioPanel('adaptiveHighRecoverEntryMarginRow'); }
function getAdaptiveHighRecoverEntryMarginSymbolEl() { return inAudioPanel('adaptiveHighRecoverEntryMarginSymbol'); }
function getAdaptiveHighRecoverEntryMarginInputEl() { return inAudioPanel('adaptiveHighRecoverEntryMarginInput'); }
function getAdaptiveUpdateIntervalCallbacksInputEl() { return inAudioPanel('adaptiveUpdateIntervalCallbacksInput'); }
function getAdaptiveLowRecoverSettleStableMsInputEl() { return inAudioPanel('adaptiveLowRecoverSettleStableMsInput'); }
function getAdaptiveLowRecoverSettleStableMsRowEl() { return inAudioPanel('adaptiveLowRecoverSettleStableMsRow'); }
function getAdaptiveLowRecoverEntryMarginMsInputEl() { return inAudioPanel('adaptiveLowRecoverEntryMarginMsInput'); }
function getAdaptiveLowRecoverEntryMarginMsRowEl() { return inAudioPanel('adaptiveLowRecoverEntryMarginMsRow'); }
function getAdaptiveLowRecoverExitMarginMsInputEl() { return inAudioPanel('adaptiveLowRecoverExitMarginMsInput'); }
function getAdaptiveLowRecoverExitMarginMsRowEl() { return inAudioPanel('adaptiveLowRecoverExitMarginMsRow'); }
function getAdaptiveLowRecoverSettleMarginMsInputEl() { return inAudioPanel('adaptiveLowRecoverSettleMarginMsInput'); }
function getAdaptiveLowRecoverSettleMarginMsRowEl() { return inAudioPanel('adaptiveLowRecoverSettleMarginMsRow'); }
function getAdaptiveLowRecoverRefillDeltaAlphaInputEl() { return inAudioPanel('adaptiveLowRecoverRefillDeltaAlphaInput'); }
function getAdaptiveLowRecoverRefillDeltaAlphaRowEl() { return inAudioPanel('adaptiveLowRecoverRefillDeltaAlphaRow'); }
function getAdaptiveControlSmoothingCutoffHzInputEl() { return inAudioPanel('adaptiveControlSmoothingCutoffHzInput'); }
function getAdaptiveControlSmoothingCutoffRowEl() { return inAudioPanel('adaptiveControlSmoothingCutoffRow'); }
function getAdaptiveControlSmoothingOrderSelectEl() { return inAudioPanel('adaptiveControlSmoothingOrderSelect'); }
function getAdaptiveControlSmoothingOrderRowEl() { return inAudioPanel('adaptiveControlSmoothingOrderRow'); }
function getAdaptiveUsePreBridgeClockToggleEl() { return inAudioPanel('adaptiveUsePreBridgeClockToggle'); }
function getAdaptiveUsePreBridgeClockRowEl() { return inAudioPanel('adaptiveUsePreBridgeClockRow'); }
function getAdaptiveUseOutputPacingToggleEl() { return inAudioPanel('adaptiveUseOutputPacingToggle'); }
function getAdaptiveUseOutputPacingRowEl() { return inAudioPanel('adaptiveUseOutputPacingRow'); }
function getAdaptiveDisableBackpressureToggleEl() { return inAudioPanel('adaptiveDisableBackpressureToggle'); }
function getAdaptiveDisableBackpressureRowEl() { return inAudioPanel('adaptiveDisableBackpressureRow'); }
function getAdaptiveResamplingAdvancedApplyBtnEl() { return inAudioPanel('adaptiveResamplingAdvancedApplyBtn'); }
function getAdaptiveResamplingAdvancedCancelBtnEl() { return inAudioPanel('adaptiveResamplingAdvancedCancelBtn'); }
function getAdaptiveBandDotEl() { return inAudioPanel('adaptiveBandDot'); }
function getAdaptiveBandTextEl() { return inAudioPanel('adaptiveBandText'); }
function getAdaptiveRuntimeStateTextEl() { return inAudioPanel('adaptiveRuntimeStateText'); }
function getAdaptivePauseBtnEl() { return inAudioPanel('adaptivePauseBtn'); }
function getAdaptiveRatioResetBtnEl() { return inAudioPanel('adaptiveRatioResetBtn'); }

export function renderAdaptiveResamplingUI() {
  const adaptiveResamplingToggleEl = getAdaptiveResamplingToggleEl();
  const adaptiveFarHardRecoverHighToggleEl = getAdaptiveFarHardRecoverHighToggleEl();
  const adaptiveFarHardRecoverLowToggleEl = getAdaptiveFarHardRecoverLowToggleEl();
  const adaptiveFarSilenceToggleEl = getAdaptiveFarSilenceToggleEl();
  const adaptiveFarSilenceRowEl = getAdaptiveFarSilenceRowEl();
  const adaptiveFarFadeRowEl = getAdaptiveFarFadeRowEl();
  const adaptiveFarFadeInMsInputEl = getAdaptiveFarFadeInMsInputEl();
  const adaptiveUpdateIntervalRowEl = getAdaptiveUpdateIntervalRowEl();
  const adaptiveKpNearInputEl = getAdaptiveKpNearInputEl();
  const adaptiveKpNearRowEl = getAdaptiveKpNearRowEl();
  const adaptiveKiInputEl = getAdaptiveKiInputEl();
  const adaptiveKiRowEl = getAdaptiveKiRowEl();
  const adaptiveIntegralDischargeRatioInputEl = getAdaptiveIntegralDischargeRatioInputEl();
  const adaptiveIntegralDischargeRowEl = getAdaptiveIntegralDischargeRowEl();
  const adaptiveMaxAdjustInputEl = getAdaptiveMaxAdjustInputEl();
  const adaptiveMaxAdjustRowEl = getAdaptiveMaxAdjustRowEl();
  const adaptiveHighRecoverEntryMarginRowEl = getAdaptiveHighRecoverEntryMarginRowEl();
  const adaptiveHighRecoverEntryMarginSymbolEl = getAdaptiveHighRecoverEntryMarginSymbolEl();
  const adaptiveHighRecoverEntryMarginInputEl = getAdaptiveHighRecoverEntryMarginInputEl();
  const adaptiveUpdateIntervalCallbacksInputEl = getAdaptiveUpdateIntervalCallbacksInputEl();
  const adaptiveLowRecoverSettleStableMsInputEl = getAdaptiveLowRecoverSettleStableMsInputEl();
  const adaptiveLowRecoverSettleStableMsRowEl = getAdaptiveLowRecoverSettleStableMsRowEl();
  const adaptiveLowRecoverEntryMarginMsInputEl = getAdaptiveLowRecoverEntryMarginMsInputEl();
  const adaptiveLowRecoverEntryMarginMsRowEl = getAdaptiveLowRecoverEntryMarginMsRowEl();
  const adaptiveLowRecoverExitMarginMsInputEl = getAdaptiveLowRecoverExitMarginMsInputEl();
  const adaptiveLowRecoverExitMarginMsRowEl = getAdaptiveLowRecoverExitMarginMsRowEl();
  const adaptiveLowRecoverSettleMarginMsInputEl = getAdaptiveLowRecoverSettleMarginMsInputEl();
  const adaptiveLowRecoverSettleMarginMsRowEl = getAdaptiveLowRecoverSettleMarginMsRowEl();
  const adaptiveLowRecoverRefillDeltaAlphaInputEl = getAdaptiveLowRecoverRefillDeltaAlphaInputEl();
  const adaptiveLowRecoverRefillDeltaAlphaRowEl = getAdaptiveLowRecoverRefillDeltaAlphaRowEl();
  const adaptiveControlSmoothingCutoffHzInputEl = getAdaptiveControlSmoothingCutoffHzInputEl();
  const adaptiveControlSmoothingCutoffRowEl = getAdaptiveControlSmoothingCutoffRowEl();
  const adaptiveControlSmoothingOrderSelectEl = getAdaptiveControlSmoothingOrderSelectEl();
  const adaptiveControlSmoothingOrderRowEl = getAdaptiveControlSmoothingOrderRowEl();
  const adaptiveUsePreBridgeClockToggleEl = getAdaptiveUsePreBridgeClockToggleEl();
  const adaptiveUsePreBridgeClockRowEl = getAdaptiveUsePreBridgeClockRowEl();
  const adaptiveUseOutputPacingToggleEl = getAdaptiveUseOutputPacingToggleEl();
  const adaptiveUseOutputPacingRowEl = getAdaptiveUseOutputPacingRowEl();
  const adaptiveDisableBackpressureToggleEl = getAdaptiveDisableBackpressureToggleEl();
  const adaptiveDisableBackpressureRowEl = getAdaptiveDisableBackpressureRowEl();
  const adaptiveResamplingAdvancedApplyBtnEl = getAdaptiveResamplingAdvancedApplyBtnEl();
  const adaptiveResamplingAdvancedCancelBtnEl = getAdaptiveResamplingAdvancedCancelBtnEl();
  const adaptiveBandDotEl = getAdaptiveBandDotEl();
  const adaptiveBandTextEl = getAdaptiveBandTextEl();
  const adaptiveRuntimeStateTextEl = getAdaptiveRuntimeStateTextEl();
  const adaptivePauseBtnEl = getAdaptivePauseBtnEl();
  const adaptiveRatioResetBtnEl = getAdaptiveRatioResetBtnEl();
  if (!adaptiveResamplingToggleEl) return;
  const hasAudioDomain = hasProducerDomain('audio');
  const farModeEnabled =
    app.adaptiveResamplingHardRecoverHighInFarMode === true
    || app.adaptiveResamplingHardRecoverLowInFarMode === true
    || app.adaptiveResamplingForceSilenceInFarMode === true;
  const adaptiveEnabled = hasAudioDomain && app.adaptiveResamplingEnabled === true;
  adaptiveResamplingToggleEl.checked = app.adaptiveResamplingEnabled === true;
  adaptiveResamplingToggleEl.disabled = !hasAudioDomain;
  if (adaptiveFarHardRecoverHighToggleEl) {
    adaptiveFarHardRecoverHighToggleEl.checked = app.adaptiveResamplingHardRecoverHighInFarMode === true;
    adaptiveFarHardRecoverHighToggleEl.disabled = !hasAudioDomain;
  }
  if (adaptiveFarHardRecoverLowToggleEl) {
    adaptiveFarHardRecoverLowToggleEl.checked = app.adaptiveResamplingHardRecoverLowInFarMode === true;
    adaptiveFarHardRecoverLowToggleEl.disabled = !hasAudioDomain;
  }
  if (adaptiveFarSilenceToggleEl) {
    adaptiveFarSilenceToggleEl.checked = app.adaptiveResamplingForceSilenceInFarMode === true;
    adaptiveFarSilenceToggleEl.disabled = !hasAudioDomain;
  }
  if (adaptiveFarSilenceRowEl) {
    adaptiveFarSilenceRowEl.classList.toggle('adaptive-param-disabled', false);
  }
  const farSilenceEnabled = app.adaptiveResamplingForceSilenceInFarMode === true;
  if (adaptiveFarFadeRowEl) {
    adaptiveFarFadeRowEl.classList.toggle('adaptive-param-disabled', !farSilenceEnabled);
  }
  if (adaptiveFarFadeInMsInputEl) {
    adaptiveFarFadeInMsInputEl.disabled = !farSilenceEnabled;
  }
  if (adaptiveUpdateIntervalRowEl) {
    adaptiveUpdateIntervalRowEl.classList.toggle('adaptive-param-disabled', !adaptiveEnabled);
  }
  if (adaptiveUpdateIntervalCallbacksInputEl) {
    adaptiveUpdateIntervalCallbacksInputEl.disabled = !adaptiveEnabled;
  }
  if (adaptiveMaxAdjustRowEl) {
    adaptiveMaxAdjustRowEl.classList.toggle('adaptive-param-disabled', !adaptiveEnabled);
  }
  if (adaptiveMaxAdjustInputEl) {
    adaptiveMaxAdjustInputEl.disabled = !adaptiveEnabled;
  }
  if (adaptiveKpNearRowEl) {
    adaptiveKpNearRowEl.classList.toggle('adaptive-param-disabled', !adaptiveEnabled);
  }
  if (adaptiveKpNearInputEl) {
    adaptiveKpNearInputEl.disabled = !adaptiveEnabled;
  }
  if (adaptiveKiRowEl) {
    adaptiveKiRowEl.classList.toggle('adaptive-param-disabled', !adaptiveEnabled);
  }
  if (adaptiveKiInputEl) {
    adaptiveKiInputEl.disabled = !adaptiveEnabled;
  }
  if (adaptiveIntegralDischargeRowEl) {
    adaptiveIntegralDischargeRowEl.classList.toggle('adaptive-param-disabled', !adaptiveEnabled);
  }
  if (adaptiveIntegralDischargeRatioInputEl) {
    adaptiveIntegralDischargeRatioInputEl.disabled = !adaptiveEnabled;
  }
  if (adaptiveHighRecoverEntryMarginInputEl) {
    adaptiveHighRecoverEntryMarginInputEl.disabled = !farModeEnabled;
  }
  if (adaptiveHighRecoverEntryMarginRowEl) {
    adaptiveHighRecoverEntryMarginRowEl.classList.toggle('adaptive-param-disabled', !farModeEnabled);
  }
  if (adaptiveHighRecoverEntryMarginSymbolEl) {
    adaptiveHighRecoverEntryMarginSymbolEl.style.opacity = farModeEnabled ? '1' : '0.42';
  }
  if (adaptiveFarFadeInMsInputEl && !app.adaptiveFarFadeInMsEditing && !app.adaptiveFarFadeInMsDirty) {
    adaptiveFarFadeInMsInputEl.value = String(Math.max(0, Math.round(app.adaptiveResamplingFarModeReturnFadeInMs ?? 0)));
  }
  if (adaptiveKpNearInputEl && !app.adaptiveKpNearEditing && !app.adaptiveKpNearDirty) {
    adaptiveKpNearInputEl.value = app.adaptiveResamplingKpNear === null ? '' : Number(app.adaptiveResamplingKpNear).toFixed(3);
  }
  if (adaptiveKiInputEl && !app.adaptiveKiEditing && !app.adaptiveKiDirty) {
    adaptiveKiInputEl.value = app.adaptiveResamplingKi === null ? '' : Number(app.adaptiveResamplingKi).toFixed(3);
  }
  if (
    adaptiveIntegralDischargeRatioInputEl &&
    !app.adaptiveIntegralDischargeRatioEditing &&
    !app.adaptiveIntegralDischargeRatioDirty
  ) {
    adaptiveIntegralDischargeRatioInputEl.value =
      app.adaptiveResamplingIntegralDischargeRatio === null
        ? ''
        : Number(app.adaptiveResamplingIntegralDischargeRatio).toFixed(3);
  }
  if (adaptiveMaxAdjustInputEl && !app.adaptiveMaxAdjustEditing && !app.adaptiveMaxAdjustDirty) {
    adaptiveMaxAdjustInputEl.value = app.adaptiveResamplingMaxAdjust === null ? '' : Math.round(Number(app.adaptiveResamplingMaxAdjust) * 1_000_000);
  }
  if (adaptiveHighRecoverEntryMarginInputEl && !app.adaptiveHighRecoverEntryMarginEditing && !app.adaptiveHighRecoverEntryMarginDirty) {
    adaptiveHighRecoverEntryMarginInputEl.value = app.adaptiveResamplingHighRecoverEntryMarginMs === null ? '' : String(Math.max(1, Math.round(app.adaptiveResamplingHighRecoverEntryMarginMs)));
  }
  if (adaptiveUpdateIntervalCallbacksInputEl && !app.adaptiveUpdateIntervalCallbacksEditing && !app.adaptiveUpdateIntervalCallbacksDirty) {
    adaptiveUpdateIntervalCallbacksInputEl.value = app.adaptiveResamplingUpdateIntervalCallbacks === null ? '' : String(Math.max(1, Math.round(app.adaptiveResamplingUpdateIntervalCallbacks)));
  }
  if (adaptiveLowRecoverSettleStableMsRowEl) {
    adaptiveLowRecoverSettleStableMsRowEl.classList.toggle('adaptive-param-disabled', !hasAudioDomain);
  }
  if (adaptiveLowRecoverSettleStableMsInputEl) {
    adaptiveLowRecoverSettleStableMsInputEl.disabled = !hasAudioDomain;
    if (!app.adaptiveLowRecoverSettleStableMsEditing && !app.adaptiveLowRecoverSettleStableMsDirty) {
      adaptiveLowRecoverSettleStableMsInputEl.value = app.adaptiveResamplingLowRecoverSettleStableMs === null ? '' : String(Math.max(0, Math.round(Number(app.adaptiveResamplingLowRecoverSettleStableMs))));
    }
  }
  if (adaptiveLowRecoverEntryMarginMsRowEl) {
    adaptiveLowRecoverEntryMarginMsRowEl.classList.toggle('adaptive-param-disabled', !hasAudioDomain);
  }
  if (adaptiveLowRecoverEntryMarginMsInputEl) {
    adaptiveLowRecoverEntryMarginMsInputEl.disabled = !hasAudioDomain;
    if (!app.adaptiveLowRecoverEntryMarginMsEditing && !app.adaptiveLowRecoverEntryMarginMsDirty) {
      adaptiveLowRecoverEntryMarginMsInputEl.value = app.adaptiveResamplingLowRecoverEntryMarginMs === null ? '' : Number(app.adaptiveResamplingLowRecoverEntryMarginMs).toFixed(1);
    }
  }
  if (adaptiveLowRecoverExitMarginMsRowEl) {
    adaptiveLowRecoverExitMarginMsRowEl.classList.toggle('adaptive-param-disabled', !hasAudioDomain);
  }
  if (adaptiveLowRecoverExitMarginMsInputEl) {
    adaptiveLowRecoverExitMarginMsInputEl.disabled = !hasAudioDomain;
    if (!app.adaptiveLowRecoverExitMarginMsEditing && !app.adaptiveLowRecoverExitMarginMsDirty) {
      adaptiveLowRecoverExitMarginMsInputEl.value = app.adaptiveResamplingLowRecoverExitMarginMs === null ? '' : Number(app.adaptiveResamplingLowRecoverExitMarginMs).toFixed(1);
    }
  }
  if (adaptiveLowRecoverSettleMarginMsRowEl) {
    adaptiveLowRecoverSettleMarginMsRowEl.classList.toggle('adaptive-param-disabled', !hasAudioDomain);
  }
  if (adaptiveLowRecoverSettleMarginMsInputEl) {
    adaptiveLowRecoverSettleMarginMsInputEl.disabled = !hasAudioDomain;
    if (!app.adaptiveLowRecoverSettleMarginMsEditing && !app.adaptiveLowRecoverSettleMarginMsDirty) {
      adaptiveLowRecoverSettleMarginMsInputEl.value = app.adaptiveResamplingLowRecoverSettleMarginMs === null ? '' : Number(app.adaptiveResamplingLowRecoverSettleMarginMs).toFixed(1);
    }
  }
  if (adaptiveLowRecoverRefillDeltaAlphaRowEl) {
    adaptiveLowRecoverRefillDeltaAlphaRowEl.classList.toggle('adaptive-param-disabled', !hasAudioDomain);
  }
  if (adaptiveLowRecoverRefillDeltaAlphaInputEl) {
    adaptiveLowRecoverRefillDeltaAlphaInputEl.disabled = !hasAudioDomain;
    if (!app.adaptiveLowRecoverRefillDeltaAlphaEditing && !app.adaptiveLowRecoverRefillDeltaAlphaDirty) {
      adaptiveLowRecoverRefillDeltaAlphaInputEl.value = app.adaptiveResamplingLowRecoverRefillDeltaAlpha === null ? '' : Number(app.adaptiveResamplingLowRecoverRefillDeltaAlpha).toFixed(2);
    }
  }
  if (adaptiveControlSmoothingCutoffRowEl) {
    adaptiveControlSmoothingCutoffRowEl.classList.toggle('adaptive-param-disabled', !hasAudioDomain);
  }
  if (adaptiveControlSmoothingCutoffHzInputEl) {
    adaptiveControlSmoothingCutoffHzInputEl.disabled = !hasAudioDomain;
    if (!app.adaptiveControlSmoothingCutoffHzEditing && !app.adaptiveControlSmoothingCutoffHzDirty) {
      adaptiveControlSmoothingCutoffHzInputEl.value =
        app.adaptiveResamplingControlSmoothingCutoffHz === null
          ? ''
          : Number(app.adaptiveResamplingControlSmoothingCutoffHz).toFixed(3);
    }
  }
  if (adaptiveControlSmoothingOrderRowEl) {
    adaptiveControlSmoothingOrderRowEl.classList.toggle('adaptive-param-disabled', !hasAudioDomain);
  }
  if (adaptiveControlSmoothingOrderSelectEl) {
    adaptiveControlSmoothingOrderSelectEl.disabled = !hasAudioDomain;
    if (!app.adaptiveControlSmoothingOrderDirty) {
      adaptiveControlSmoothingOrderSelectEl.value = String(
        app.adaptiveResamplingControlSmoothingOrder ?? 1,
      );
    }
  }
  if (adaptiveUsePreBridgeClockRowEl) {
    adaptiveUsePreBridgeClockRowEl.classList.toggle('adaptive-param-disabled', !hasAudioDomain);
  }
  if (adaptiveUsePreBridgeClockToggleEl) {
    adaptiveUsePreBridgeClockToggleEl.disabled = !hasAudioDomain;
    adaptiveUsePreBridgeClockToggleEl.checked =
      app.adaptiveResamplingUsePreBridgeClock === true;
  }
  if (adaptiveUseOutputPacingRowEl) {
    adaptiveUseOutputPacingRowEl.classList.toggle('adaptive-param-disabled', !hasAudioDomain);
  }
  if (adaptiveUseOutputPacingToggleEl) {
    adaptiveUseOutputPacingToggleEl.disabled = !hasAudioDomain;
    adaptiveUseOutputPacingToggleEl.checked =
      app.adaptiveResamplingUseOutputPacing === true;
  }
  if (adaptiveDisableBackpressureRowEl) {
    adaptiveDisableBackpressureRowEl.classList.toggle('adaptive-param-disabled', !hasAudioDomain);
  }
  if (adaptiveDisableBackpressureToggleEl) {
    adaptiveDisableBackpressureToggleEl.disabled = !hasAudioDomain;
    adaptiveDisableBackpressureToggleEl.checked =
      app.adaptiveResamplingDisableBackpressure === true;
  }
  if (adaptiveBandTextEl) {
    adaptiveBandTextEl.textContent = app.adaptiveResamplingBand ?? '—';
  }
  if (adaptiveRuntimeStateTextEl) {
    adaptiveRuntimeStateTextEl.textContent = app.adaptiveResamplingState ?? '—';
  }
  if (adaptiveBandDotEl) {
    adaptiveBandDotEl.style.background =
      app.adaptiveResamplingBand === 'hard'
        ? '#ff4d4d'
        :
      app.adaptiveResamplingBand === 'far'
        ? '#ff9a5c'
        : app.adaptiveResamplingBand === 'near'
          ? '#52e2a2'
          : 'rgba(255,255,255,0.25)';
  }
  const isPaused = app.adaptiveResamplingPaused === true;
  if (adaptivePauseBtnEl) {
    adaptivePauseBtnEl.textContent = isPaused ? `▶ ${t('adaptive.resume')}` : `⏸ ${t('adaptive.pause')}`;
    adaptivePauseBtnEl.style.background = isPaused ? 'rgba(255,180,0,0.18)' : 'rgba(255,255,255,0.08)';
    adaptivePauseBtnEl.style.borderColor = isPaused ? 'rgba(255,180,0,0.5)' : 'rgba(255,255,255,0.2)';
    adaptivePauseBtnEl.style.color = isPaused ? '#ffd87a' : '#d9ecff';
    adaptivePauseBtnEl.disabled = !adaptiveEnabled;
    adaptivePauseBtnEl.style.opacity = adaptiveEnabled ? '1' : '0.45';
    adaptivePauseBtnEl.style.cursor = adaptiveEnabled ? 'pointer' : 'default';
  }
  if (adaptiveRatioResetBtnEl) {
    adaptiveRatioResetBtnEl.style.display = adaptiveEnabled && isPaused ? '' : 'none';
    adaptiveRatioResetBtnEl.disabled = !adaptiveEnabled;
    adaptiveRatioResetBtnEl.style.opacity = adaptiveEnabled ? '1' : '0.45';
    adaptiveRatioResetBtnEl.style.cursor = adaptiveEnabled ? 'pointer' : 'default';
  }
  const adaptiveDirty =
    app.adaptiveKpNearDirty ||
    app.adaptiveKiDirty ||
    app.adaptiveIntegralDischargeRatioDirty ||
    app.adaptiveMaxAdjustDirty ||
    app.adaptiveHighRecoverEntryMarginDirty ||
    app.adaptiveUpdateIntervalCallbacksDirty ||
    app.adaptiveFarFadeInMsDirty ||
    app.adaptiveLowRecoverSettleStableMsDirty ||
    app.adaptiveLowRecoverEntryMarginMsDirty ||
    app.adaptiveLowRecoverExitMarginMsDirty ||
    app.adaptiveLowRecoverSettleMarginMsDirty ||
    app.adaptiveLowRecoverRefillDeltaAlphaDirty ||
    app.adaptiveControlSmoothingCutoffHzDirty ||
    app.adaptiveControlSmoothingOrderDirty;
  if (adaptiveResamplingAdvancedApplyBtnEl) {
    adaptiveResamplingAdvancedApplyBtnEl.disabled = !adaptiveDirty;
    adaptiveResamplingAdvancedApplyBtnEl.style.opacity = adaptiveDirty ? '1' : '0.45';
    adaptiveResamplingAdvancedApplyBtnEl.style.cursor = adaptiveDirty ? 'pointer' : 'default';
  }
  if (adaptiveResamplingAdvancedCancelBtnEl) {
    adaptiveResamplingAdvancedCancelBtnEl.disabled = !adaptiveDirty;
    adaptiveResamplingAdvancedCancelBtnEl.style.opacity = adaptiveDirty ? '1' : '0.45';
    adaptiveResamplingAdvancedCancelBtnEl.style.cursor = adaptiveDirty ? 'pointer' : 'default';
  }
}

export function updateAdaptiveResamplingUI() {
  dirty.adaptiveResampling = true;
  dirty.resample = true;
  scheduleUIFlush();
}

export function resetAdaptiveResamplingAdvancedDirtyState() {
  app.adaptiveKpNearDirty = false;
  app.adaptiveKpNearEditing = false;
  app.adaptiveKiDirty = false;
  app.adaptiveKiEditing = false;
  app.adaptiveIntegralDischargeRatioDirty = false;
  app.adaptiveIntegralDischargeRatioEditing = false;
  app.adaptiveMaxAdjustDirty = false;
  app.adaptiveMaxAdjustEditing = false;
  app.adaptiveHighRecoverEntryMarginDirty = false;
  app.adaptiveHighRecoverEntryMarginEditing = false;
  app.adaptiveUpdateIntervalCallbacksDirty = false;
  app.adaptiveUpdateIntervalCallbacksEditing = false;
  app.adaptiveFarFadeInMsDirty = false;
  app.adaptiveFarFadeInMsEditing = false;
  app.adaptiveLowRecoverSettleStableMsDirty = false;
  app.adaptiveLowRecoverSettleStableMsEditing = false;
  app.adaptiveLowRecoverEntryMarginMsDirty = false;
  app.adaptiveLowRecoverEntryMarginMsEditing = false;
  app.adaptiveLowRecoverExitMarginMsDirty = false;
  app.adaptiveLowRecoverExitMarginMsEditing = false;
  app.adaptiveLowRecoverSettleMarginMsDirty = false;
  app.adaptiveLowRecoverSettleMarginMsEditing = false;
  app.adaptiveLowRecoverRefillDeltaAlphaDirty = false;
  app.adaptiveLowRecoverRefillDeltaAlphaEditing = false;
  app.adaptiveControlSmoothingCutoffHzDirty = false;
  app.adaptiveControlSmoothingCutoffHzEditing = false;
  app.adaptiveControlSmoothingOrderDirty = false;
}
