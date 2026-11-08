// Only for a verified active-HIGH Grove button connected to D2.
// This is a raw-state check, NOT the final one-frame release protocol.
void setup() {
  Serial.begin(115200);
  pinMode(2, INPUT);
}
void loop() {
  Serial.println(digitalRead(2) == HIGH ? 1 : 0);
  delay(100);
}
