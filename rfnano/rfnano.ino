#include <RCSwitch.h>

RCSwitch rf = RCSwitch();

const byte RF_RX_INTERRUPT = 0; // D2 en Arduino Nano/Uno
const byte RF_TX_PIN = 10;

const unsigned long gateCode = 179973589;
const byte gateBits = 28;
const byte gateProtocol = 6;
const unsigned int gatePulseLength = 350;

const unsigned long pulseDuration = 250;

bool pulseActive = false;
unsigned long pulseStart = 0;
unsigned long lastReceivePrint = 0;

void enableRx() {
  rf.enableReceive(RF_RX_INTERRUPT);
}

void enableTx() {
  rf.disableReceive();
  rf.enableTransmit(RF_TX_PIN);
  rf.setProtocol(gateProtocol);
  rf.setPulseLength(gatePulseLength);
}

void receiveRf() {
  if (!rf.available()) {
    return;
  }

  unsigned long code = rf.getReceivedValue();

  if (code == 0) {
    Serial.println("RX: codigo desconocido");
  } else {
    Serial.print("RX Codigo: ");
    Serial.println(code);

    Serial.print("RX Bits: ");
    Serial.println(rf.getReceivedBitlength());

    Serial.print("RX Protocolo: ");
    Serial.println(rf.getReceivedProtocol());

    Serial.print("RX Delay: ");
    Serial.println(rf.getReceivedDelay());
  }

  Serial.println("---");
  rf.resetAvailable();
  lastReceivePrint = millis();
}

void startPulse() {
  pulseActive = true;
  pulseStart = millis();
  enableTx();
  Serial.println("ACK:PULSE");
}

void stopPulse() {
  pulseActive = false;
  Serial.println("ACK:PULSE_END");
  enableRx();
}

void transmitRf() {
  rf.send(gateCode, gateBits);
}

void checkSerial() {
  if (!Serial.available()) {
    return;
  }

  String cmd = Serial.readStringUntil('\n');
  cmd.trim();

  if (cmd == "CMD:PULSE") {
    startPulse();
    return;
  }

  if (cmd == "CMD:RX") {
    pulseActive = false;
    enableRx();
    Serial.println("ACK:RX");
    return;
  }

  if (cmd == "CMD:STATUS") {
    Serial.print("STATUS:");
    Serial.println(pulseActive ? "TX" : "RX");
    return;
  }

  Serial.print("ERR:CMD_UNKNOWN:");
  Serial.println(cmd);
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(50);

  enableRx();

  Serial.println("RF Nano iniciado");
  Serial.println("RX en D2 / TX en D10");
}

void loop() {
  checkSerial();

  if (pulseActive) {
    transmitRf();

    if (millis() - pulseStart >= pulseDuration) {
      stopPulse();
    }

    return;
  }

  receiveRf();
}
